"""Student-facing portal routes — requires student authentication."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import aiofiles
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import and_, func, nullslast, or_, select
from sqlalchemy.orm import selectinload

from submissions_checker.api.dependencies import AppSettings, DBSession, StudentId, StudentUser
from submissions_checker.api.schemas.student_portal import (
    AssignmentDetail,
    AssignmentRow,
    ContentFile,
    SquadMemberState,
    SquadPanel,
    SubjectCard,
)
from submissions_checker.core import metrics
from submissions_checker.core.i18n import get_vocab
from submissions_checker.core.templates import render
from submissions_checker.db.models import (
    OutboxMessage,
    QuizAttempt,
    Student,
    StudentAssignment,
    Subject,
    SubjectsAssignment,
    SubjectsStudents,
    Submission,
    SubmissionSourceType,
    SubmissionStatus,
)
from submissions_checker.db.models.enums import (
    NotificationCase,
    NotificationMethod,
    OutboxEventType,
    OutboxMessageState,
    QuizAttemptStatus,
)
from submissions_checker.db.models.notification_preference import NotificationPreference
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.services import squads
from submissions_checker.services.audit import audit
from submissions_checker.services.quiz_grants import effective_max_attempts
from submissions_checker.services.similarity import compare_zip_files
from submissions_checker.workers.tasks.check_tasks import (
    execute_check_task,
    is_quiz_first_assignment,
)

router = APIRouter(prefix="/portal", tags=["student-portal"])

UPLOADS_DIR = Path("uploads")
UPLOADS_DIR.mkdir(exist_ok=True)

# Statuses that a background worker is going to move on its own, without anyone touching the
# page. A submission sitting in one of these is why the assignment page has to watch itself:
# the upload redirects here while the status is still PENDING, and the quiz link only appears
# once the check task has advanced it. Human-gated waits (teacher review) are deliberately not
# in here — nothing is going to change in the next few seconds, so polling them is noise.
TRANSIENT_STATUSES = frozenset(
    {
        SubmissionStatus.PENDING,
        SubmissionStatus.VALIDATING,
        SubmissionStatus.TESTING,
        SubmissionStatus.AWAITING_AI_REVIEW,
        SubmissionStatus.AI_REVIEWING,
    }
)


async def student_needs_consent(db: DBSession, student_id: int) -> bool:
    """True if the student has not yet acknowledged the proctoring recording notice."""
    consented = await db.scalar(
        select(Student.recording_consent_at).where(Student.id == student_id)
    )
    return consented is None


@router.get("/consent", response_class=HTMLResponse, response_model=None)
async def show_consent(
    request: Request,
    db: DBSession,
    current_user: StudentUser,
    student_id: StudentId,
    settings: AppSettings,
) -> HTMLResponse | RedirectResponse:
    if not await student_needs_consent(db, student_id):
        return RedirectResponse(url="/portal", status_code=303)
    vocab = get_vocab(request.cookies.get("lang"))
    notice = settings.recording_consent_notice or vocab["consent"]["notice_text"]
    return render(
        request,
        "student_consent.html",
        {
            "current_user": current_user,
            "notice": notice,
        },
    )


@router.post("/consent")
async def accept_consent(
    db: DBSession, current_user: StudentUser, student_id: StudentId
) -> RedirectResponse:
    student = await db.get(Student, student_id)
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")
    if student.recording_consent_at is None:
        student.recording_consent_at = datetime.now(UTC)
        await db.commit()
    return RedirectResponse(url="/portal", status_code=303)


@router.get("", response_class=HTMLResponse, response_model=None)
async def subjects_grid(
    request: Request, db: DBSession, current_user: StudentUser, student_id: StudentId
) -> HTMLResponse | RedirectResponse:
    student = await db.get(Student, student_id)
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")
    if student.recording_consent_at is None:
        return RedirectResponse(url="/portal/consent", status_code=303)

    enrolled_result = await db.execute(
        select(Subject)
        .join(SubjectsStudents, SubjectsStudents.subject_id == Subject.id)
        .where(SubjectsStudents.student_id == student_id)
        .order_by(Subject.name)
    )
    subjects = enrolled_result.scalars().all()

    subject_ids = [s.id for s in subjects]
    totals: dict[int, int] = {}
    done: dict[int, int] = {}

    if subject_ids:
        totals_result = await db.execute(
            select(SubjectsAssignment.subject_id, func.count().label("total"))
            .where(SubjectsAssignment.subject_id.in_(subject_ids))
            .group_by(SubjectsAssignment.subject_id)
        )
        totals = {row.subject_id: row.total for row in totals_result}

        done_result = await db.execute(
            select(SubjectsAssignment.subject_id, func.count().label("done"))
            .join(
                StudentAssignment, StudentAssignment.subjects_assignment_id == SubjectsAssignment.id
            )
            .where(
                StudentAssignment.student_id == student_id,
                SubjectsAssignment.subject_id.in_(subject_ids),
                StudentAssignment.grade.is_not(None),
            )
            .group_by(SubjectsAssignment.subject_id)
        )
        done = {row.subject_id: row.done for row in done_result}

    subject_cards = [
        SubjectCard(
            id=s.id,
            name=s.name,
            description=s.description,
            total_assignments=totals.get(s.id, 0),
            done_assignments=done.get(s.id, 0),
            grid_picture_url=s.grid_picture_url,
        )
        for s in subjects
    ]

    return render(
        request,
        "subjects.html",
        {"current_user": current_user, "student": student, "subjects": subject_cards},
    )


@router.get("/subjects/{subject_id}", response_class=HTMLResponse)
async def assignments_list(
    request: Request,
    subject_id: int,
    db: DBSession,
    current_user: StudentUser,
    student_id: StudentId,
) -> HTMLResponse:
    student = await db.get(Student, student_id)
    if student is None:
        raise HTTPException(status_code=404)

    enrollment_result = await db.execute(
        select(SubjectsStudents).where(
            SubjectsStudents.student_id == student_id,
            SubjectsStudents.subject_id == subject_id,
        )
    )
    if enrollment_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Not enrolled in this subject")

    subject = await db.get(Subject, subject_id)
    if subject is None:
        raise HTTPException(status_code=404)

    sa_result = await db.execute(
        select(StudentAssignment)
        .join(SubjectsAssignment, StudentAssignment.subjects_assignment_id == SubjectsAssignment.id)
        .where(
            StudentAssignment.student_id == student_id,
            SubjectsAssignment.subject_id == subject_id,
        )
        .options(
            selectinload(StudentAssignment.subjects_assignment),
            selectinload(StudentAssignment.submissions),
        )
        .order_by(nullslast(SubjectsAssignment.deadline))
    )
    student_assignments = sa_result.scalars().all()

    squad_max = subject.squad_max_size
    locked_squad = await squads.active_squad(db, subject_id, student_id) if squad_max else None

    assignment_rows = []
    for sa in student_assignments:
        latest_sub = (
            await squads.latest_submission(db, sa)
            if locked_squad
            else (max(sa.submissions, key=lambda s: s.created_at) if sa.submissions else None)
        )
        waiting_for: list[str] = []
        squad_name = None
        if latest_sub is not None and latest_sub.squad_id is not None and locked_squad is not None:
            squad_name = squads.display_name(locked_squad)
            if latest_sub.status == SubmissionStatus.QUIZ_SENT:
                states = await squads.member_quiz_states(db, latest_sub)
                me = next((m for m in states if m.student_id == student_id), None)
                if me is not None and me.passed:
                    waiting_for = [m.full_name for m in states if not m.passed]
        assignment_rows.append(
            AssignmentRow(
                student_assignment_id=sa.id,
                title=sa.subjects_assignment.title,
                deadline=sa.subjects_assignment.deadline,
                grade=sa.grade,
                min_grade=sa.subjects_assignment.min_grade,
                max_grade=sa.subjects_assignment.max_grade,
                submission_status=latest_sub.status if latest_sub else None,
                squad_name=squad_name,
                waiting_for=waiting_for,
            )
        )

    squad_state = "disabled"
    pending = None
    classmates: list[Student] = []
    if squad_max is not None:
        if locked_squad is not None:
            squad_state = "locked"
        else:
            pending = await squads.pending_state(db, subject_id, student_id)
            if pending.squad is not None:
                squad_state = "pending"
            elif pending.incoming:
                squad_state = "incoming"
            elif await squads.eligibility(db, subject_id, student_id) is None:
                squad_state = "eligible"
                classmates = await squads.eligible_classmates(db, subject_id, student_id)
            else:
                squad_state = "ineligible"

    return render(
        request,
        "assignments.html",
        {
            "current_user": current_user,
            "student": student,
            "subject": subject,
            "assignments": assignment_rows,
            "squad_enabled": squad_max is not None,
            "squad_max_size": squad_max,
            "squad_state": squad_state,
            "squad": locked_squad,
            "squad_name": squads.display_name(locked_squad) if locked_squad else None,
            "squad_members": [m.student.full_name for m in locked_squad.members]
            if locked_squad
            else [],
            "pending": pending,
            "classmates": classmates,
            "squad_error": request.query_params.get("squad_error"),
            "squad_flash": request.query_params.get("squad_flash"),
        },
    )


@router.get("/subjects/{subject_id}/assignments/{sa_id}", response_class=HTMLResponse)
async def assignment_detail(
    request: Request,
    subject_id: int,
    sa_id: int,
    db: DBSession,
    current_user: StudentUser,
    student_id: StudentId,
) -> HTMLResponse:
    sa_result = await db.execute(
        select(StudentAssignment)
        .where(
            StudentAssignment.id == sa_id,
            StudentAssignment.student_id == student_id,
        )
        .options(
            selectinload(StudentAssignment.subjects_assignment),
            selectinload(StudentAssignment.submissions),
        )
    )
    sa = sa_result.scalar_one_or_none()
    if sa is None:
        raise HTTPException(status_code=404)

    latest_sub = await squads.latest_submission(db, sa)

    # Quiz attempt metadata: count this student's used attempts on the squad-resolved latest
    # submission — NOT the raw sa_id, since for a squad member who didn't upload, the shared
    # submission's students_assignment_id belongs to whoever did. Legacy NULL student_id rows
    # predate per-student attempts and belong to solo submissions by this same student, so
    # they still count here.
    quiz_attempts_used = 0
    if latest_sub is not None:
        attempts_used_result = await db.execute(
            select(func.count(QuizAttempt.id)).where(
                and_(
                    QuizAttempt.submission_id == latest_sub.id,
                    QuizAttempt.status.in_(
                        [QuizAttemptStatus.COMPLETED, QuizAttemptStatus.TIMED_OUT]
                    ),
                    or_(QuizAttempt.student_id == student_id, QuizAttempt.student_id.is_(None)),
                )
            )
        )
        quiz_attempts_used = attempts_used_result.scalar_one() or 0

    # Latest attempt id (for result link) and max_attempts from config
    quiz_attempt_id: int | None = None
    quiz_max_attempts: int | None = None
    if latest_sub:
        latest_attempt_result = await db.execute(
            select(QuizAttempt)
            .where(
                QuizAttempt.submission_id == latest_sub.id,
                or_(QuizAttempt.student_id == student_id, QuizAttempt.student_id.is_(None)),
            )
            .order_by(QuizAttempt.started_at.desc())
            .limit(1)
        )
        latest_attempt = latest_attempt_result.scalar_one_or_none()
        if latest_attempt:
            quiz_attempt_id = latest_attempt.id
            quiz_max_attempts = latest_attempt.config_snapshot.get("max_quiz_attempts")
        elif latest_sub.plugin_config_id and sa.subjects_assignment.code:
            plugin_cfg = await db.get(SubjectPluginConfig, latest_sub.plugin_config_id)
            if plugin_cfg:
                quiz_cfg = (
                    plugin_cfg.config.get("assignments", {})
                    .get(sa.subjects_assignment.code, {})
                    .get("quiz", {})
                )
                quiz_max_attempts = quiz_cfg.get("max_quiz_attempts")
    if latest_sub is not None:
        # The config's cap plus any attempts a teacher granted this student on this upload.
        quiz_max_attempts = effective_max_attempts(quiz_max_attempts, latest_sub, student_id)

    check_reason: str | None = None
    if latest_sub and latest_sub.test_results:
        check_reason = latest_sub.test_results.get("check_reason")

    raw_content_files: list[dict] = sa.subjects_assignment.content_files or []  # type: ignore[type-arg]
    content_files = [ContentFile(**cf) for cf in raw_content_files]

    # AI comment and grade breakdown are shown to the student only when the
    # assignment config opts in. Cheating/AI-generated verdicts are never exposed.
    assignment_cfg = sa.subjects_assignment.config or {}
    ai_comment: str | None = None
    if latest_sub and latest_sub.ai_review:
        if (assignment_cfg.get("ai_review") or {}).get("show_comment_to_student"):
            ai_comment = latest_sub.ai_review.get("comment")
    grade_breakdown: dict | None = None  # type: ignore[type-arg]
    if latest_sub and latest_sub.grade_breakdown:
        if (assignment_cfg.get("grading") or {}).get("show_breakdown_to_student"):
            grade_breakdown = latest_sub.grade_breakdown

    squad_panel = None
    if latest_sub is not None and latest_sub.squad_id is not None:
        squad = await squads.squad_for_submission(db, latest_sub)
        states = await squads.member_quiz_states(db, latest_sub)
        if squad is not None:
            draw = (latest_sub.source_metadata or {}).get("squad_quiz_draw") or {}
            order = draw.get("member_order") or []
            my_slice = None
            if student_id in order:
                my_slice = len(draw["slices"][order.index(student_id)])
            squad_panel = SquadPanel(
                name=squads.display_name(squad),
                members=[SquadMemberState(**vars(m)) for m in states],
                complete=all(m.passed for m in states),
                my_passed=any(m.student_id == student_id and m.passed for m in states),
                my_slice=my_slice,
                total_questions=len(draw.get("question_ids") or []) or None,
            )

    detail = AssignmentDetail(
        student_assignment_id=sa.id,
        title=sa.subjects_assignment.title,
        description=sa.subjects_assignment.description,
        deadline=sa.subjects_assignment.deadline,
        grade=sa.grade,
        min_grade=sa.subjects_assignment.min_grade,
        max_grade=sa.subjects_assignment.max_grade,
        config=sa.subjects_assignment.config,
        submission_status=latest_sub.status if latest_sub else None,
        submission_id=latest_sub.id if latest_sub else None,
        latest_submission_created_at=latest_sub.created_at if latest_sub else None,
        quiz_attempt_id=quiz_attempt_id,
        quiz_attempts_used=quiz_attempts_used,
        quiz_max_attempts=quiz_max_attempts,
        check_reason=check_reason,
        content_files=content_files,
        ai_comment=ai_comment,
        grade_breakdown=grade_breakdown,
        squad=squad_panel,
    )

    student = await db.get(Student, student_id)

    return render(
        request,
        "assignment_detail.html",
        {
            "current_user": current_user,
            "student": student,
            "subject_id": subject_id,
            "assignment": detail,
            # A page rendered mid-check is already out of date: the quiz link, the grade and
            # the result link all appear only after a worker moves the status.
            "poll_status": latest_sub is not None and latest_sub.status in TRANSIENT_STATUSES,
        },
    )


@router.get("/subjects/{subject_id}/assignments/{sa_id}/status")
async def assignment_status(
    subject_id: int,
    sa_id: int,
    db: DBSession,
    current_user: StudentUser,
    student_id: StudentId,
) -> JSONResponse:
    """Latest submission status for one assignment, for the page to poll itself with.

    ``transient`` tells the caller whether it is still worth asking again.
    """
    sa = await db.get(StudentAssignment, sa_id)
    if sa is None or sa.student_id != student_id:
        raise HTTPException(status_code=404)

    # Short-circuit to the plain single query when squads are off for this subject —
    # this endpoint is polled every few seconds while a check runs, and the squad-aware
    # path (subject + squad + member lookups) is pure overhead for the common case.
    subjects_assignment = await db.get(SubjectsAssignment, sa.subjects_assignment_id)
    squad_max = None
    if subjects_assignment is not None:
        subject = await db.get(Subject, subjects_assignment.subject_id)
        squad_max = subject.squad_max_size if subject is not None else None

    if squad_max is None:
        latest = (
            await db.execute(
                select(Submission)
                .where(Submission.students_assignment_id == sa_id)
                .order_by(Submission.created_at.desc(), Submission.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    else:
        latest = await squads.latest_submission(db, sa)
    status = latest.status if latest else None
    return JSONResponse(
        {
            "status": status.value if status else None,
            "transient": status in TRANSIENT_STATUSES,
        }
    )


@router.post("/subjects/{subject_id}/assignments/{sa_id}/submit")
async def submit_assignment(
    subject_id: int,
    sa_id: int,
    db: DBSession,
    current_user: StudentUser,
    student_id: StudentId,
    file: UploadFile = File(...),
) -> RedirectResponse:
    sa = await db.get(StudentAssignment, sa_id)
    if sa is None or sa.student_id != student_id:
        raise HTTPException(status_code=404)

    subjects_assignment = await db.get(SubjectsAssignment, sa.subjects_assignment_id)
    if subjects_assignment is None:
        raise HTTPException(status_code=404)

    # Late submission enforcement
    if subjects_assignment.deadline is not None:
        deadline = subjects_assignment.deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        if datetime.now(UTC) > deadline:
            late_policy = subjects_assignment.config.get("late_policy", "block")
            if late_policy == "block":
                raise HTTPException(
                    status_code=403,
                    detail="The submission deadline has passed. Late submissions are not accepted.",
                )

    # Resolve the submission scope: a locked squad shares one submission across every
    # member's StudentAssignment row; a pending invite blocks uploads outright, since
    # accepting or cancelling it could change who that submission covers mid-flight.
    subject_for_squad = await db.get(Subject, subjects_assignment.subject_id)
    squad = None
    if subject_for_squad is not None and subject_for_squad.squad_max_size is not None:
        if await squads.has_pending_invites(db, subjects_assignment.subject_id, student_id):
            raise HTTPException(
                status_code=409,
                detail="Squad invitation pending; answer or cancel it before submitting.",
            )
        if await squads.squad_has_pending_invites(db, subjects_assignment.subject_id, student_id):
            raise HTTPException(
                status_code=409,
                detail="Squad invitation pending; answer or cancel it before submitting.",
            )
        # Any lock state, not just locked: an unlocked squad with no pending invites
        # left is settled (nothing can still change who it covers) and may submit —
        # lock_on_submit() below locks it at the moment of that first upload.
        squad = await squads.squad_of(db, subjects_assignment.subject_id, student_id)
    scope = await squads.member_sa_ids(db, squad, subjects_assignment.id) if squad else [sa_id]

    # Block re-submission once the assignment is already passed
    completed_result = await db.execute(
        select(func.count(Submission.id)).where(
            and_(
                Submission.students_assignment_id.in_(scope),
                Submission.status == SubmissionStatus.COMPLETED,
            )
        )
    )
    if (completed_result.scalar_one() or 0) > 0:
        raise HTTPException(
            status_code=403, detail="Assignment already passed. No further submissions accepted."
        )

    # Re-submission limit
    max_submissions = subjects_assignment.config.get("max_submissions")
    if max_submissions is not None:
        count_result = await db.execute(
            select(func.count(Submission.id)).where(Submission.students_assignment_id.in_(scope))
        )
        current_count = count_result.scalar_one() or 0
        if current_count >= max_submissions:
            raise HTTPException(
                status_code=403,
                detail=f"Maximum number of submissions ({max_submissions}) reached.",
            )

    filename = file.filename or ""
    if not filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only ZIP files are accepted")

    # File size limit: 50 MB
    content = await file.read()
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="ZIP file too large (max 50 MB)")

    save_name = f"{sa_id}_{uuid.uuid4().hex}.zip"
    save_path = UPLOADS_DIR / save_name
    async with aiofiles.open(save_path, "wb") as f:
        await f.write(content)

    # Compare against all other ZIP submissions for this assignment (plagiarism detection)
    other_subs_result = await db.execute(
        select(Submission.source_metadata).where(
            Submission.students_assignment_id.in_(
                select(StudentAssignment.id).where(
                    StudentAssignment.subjects_assignment_id == sa.subjects_assignment_id,
                    StudentAssignment.id.not_in(scope),
                )
            ),
            Submission.source_type == SubmissionSourceType.ZIP_UPLOAD,
        )
    )
    max_similarity = 0.0
    for (meta,) in other_subs_result:
        other_name = meta.get("saved_as") if meta else None
        if other_name:
            other_path = UPLOADS_DIR / other_name
            if other_path.exists():
                sim = compare_zip_files(save_path, other_path)
                if sim > max_similarity:
                    max_similarity = sim

    submission = Submission(
        students_assignment_id=sa_id,
        source_type=SubmissionSourceType.ZIP_UPLOAD,
        source_metadata={
            "original_filename": filename,
            "saved_as": save_name,
            "similarity_score": round(max_similarity, 3),
        },
        status=SubmissionStatus.PENDING,
        squad_id=squad.id if squad else None,
    )
    db.add(submission)
    await db.flush()
    if squad:
        squads.lock_on_submit(squad)

    # Quiz-examined assignments have nothing to run — no sandbox, no container — so they are
    # accepted here, in the request, and the page this redirects to already carries the quiz
    # link. Everything else stays PENDING until the outbox processor picks it up.
    if await is_quiz_first_assignment(db, subjects_assignment.subject_id, subjects_assignment.code):
        await execute_check_task(db, {"submission_id": submission.id})
    else:
        db.add(
            OutboxMessage(
                event_type=OutboxEventType.RUN_CHECKS,
                state=OutboxMessageState.PENDING,
                payload={"submission_id": submission.id},
            )
        )

    await audit(
        db,
        action="student_submit",
        actor_id=current_user.user_id,
        actor_username=current_user.username,
        target_type="submission",
        target_id=submission.id,
        student_assignment_id=sa_id,
        filename=filename,
    )

    await db.commit()
    metrics.submissions_uploaded_total.inc()

    return RedirectResponse(
        url=f"/portal/subjects/{subject_id}/assignments/{sa_id}",
        status_code=303,
    )


@router.get("/summary", response_class=HTMLResponse)
async def student_summary(
    request: Request,
    db: DBSession,
    current_user: StudentUser,
    student_id: StudentId,
) -> HTMLResponse:
    """Cross-subject summary dashboard for the student."""
    student = await db.get(Student, student_id)
    if student is None:
        raise HTTPException(status_code=404)

    # All enrolled subjects
    enrolled_result = await db.execute(
        select(Subject)
        .join(SubjectsStudents, SubjectsStudents.subject_id == Subject.id)
        .where(SubjectsStudents.student_id == student_id)
        .order_by(Subject.name)
    )
    subjects = enrolled_result.scalars().all()
    subject_ids = [s.id for s in subjects]
    subject_map = {s.id: s for s in subjects}

    # All assignments across all subjects with student progress
    if subject_ids:
        all_sa_result = await db.execute(
            select(
                SubjectsAssignment,
                StudentAssignment.id.label("student_assignment_id"),
                StudentAssignment.grade,
            )
            .join(SubjectsStudents, SubjectsStudents.subject_id == SubjectsAssignment.subject_id)
            .outerjoin(
                StudentAssignment,
                and_(
                    StudentAssignment.subjects_assignment_id == SubjectsAssignment.id,
                    StudentAssignment.student_id == student_id,
                ),
            )
            .where(
                SubjectsStudents.student_id == student_id,
                SubjectsAssignment.subject_id.in_(subject_ids),
            )
            .order_by(SubjectsAssignment.deadline.asc().nullslast())
        )
        all_rows = all_sa_result.all()
    else:
        all_rows = []

    now = datetime.now(UTC)

    # Build summary stats
    graded_grades = [r.grade for r in all_rows if r.grade is not None]
    avg_grade = round(sum(graded_grades) / len(graded_grades), 1) if graded_grades else None

    upcoming_deadlines = []
    overdue = []
    for r in all_rows:
        deadline = r.SubjectsAssignment.deadline
        if deadline is None:
            continue
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        if r.grade is None:
            if deadline < now:
                overdue.append(r)
            elif (deadline - now).days <= 7:
                upcoming_deadlines.append(r)

    # Latest submissions for each student_assignment
    if any(r.student_assignment_id for r in all_rows):
        sa_ids = [r.student_assignment_id for r in all_rows if r.student_assignment_id]
        latest_sub_sq = (
            select(
                Submission.students_assignment_id,
                func.max(Submission.created_at).label("max_at"),
            )
            .where(Submission.students_assignment_id.in_(sa_ids))
            .group_by(Submission.students_assignment_id)
            .subquery()
        )
        sub_result = await db.execute(
            select(Submission).join(
                latest_sub_sq,
                and_(
                    Submission.students_assignment_id == latest_sub_sq.c.students_assignment_id,
                    Submission.created_at == latest_sub_sq.c.max_at,
                ),
            )
        )
        subs_by_sa = {s.students_assignment_id: s for s in sub_result.scalars().all()}
    else:
        subs_by_sa = {}

    # Squad subjects resolve through the squad scope instead of the student's own
    # StudentAssignment row, so one upload from any member shows up for everyone.
    for r in all_rows:
        sa_obj = r.SubjectsAssignment
        subj = subject_map.get(sa_obj.subject_id)
        if subj is not None and subj.squad_max_size is not None and r.student_assignment_id:
            sub = await squads.latest_submission_for(db, student_id, sa_obj.subject_id, sa_obj.id)
            if sub is not None:
                subs_by_sa[r.student_assignment_id] = sub

    summary_rows = []
    for r in all_rows:
        sa = r.SubjectsAssignment
        deadline = sa.deadline
        if deadline and deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        sub = subs_by_sa.get(r.student_assignment_id)
        is_overdue = deadline is not None and deadline < now and r.grade is None
        row_squad_name = None
        if sub is not None and sub.squad_id:
            row_squad = await squads.squad_for_submission(db, sub)
            row_squad_name = squads.display_name(row_squad) if row_squad is not None else None
        summary_rows.append(
            {
                "subject": subject_map.get(sa.subject_id),
                "assignment": sa,
                "student_assignment_id": r.student_assignment_id,
                "grade": r.grade,
                "submission_status": sub.status if sub else None,
                "deadline": deadline,
                "is_overdue": is_overdue,
                "squad_name": row_squad_name,
            }
        )

    return render(
        request,
        "student_summary.html",
        {
            "current_user": current_user,
            "student": student,
            "summary_rows": summary_rows,
            "avg_grade": avg_grade,
            "graded_count": len(graded_grades),
            "total_assignments": len(all_rows),
            "upcoming_deadlines": upcoming_deadlines,
            "overdue": overdue,
        },
    )


# ---------------------------------------------------------------------------
# Notification preferences
# ---------------------------------------------------------------------------

_ALL_CASES: list[tuple[NotificationCase, str]] = [
    (NotificationCase.SUBMISSION_CHECKED, "Submission Checked"),
    (NotificationCase.FEEDBACK_REQUEST, "Feedback Request"),
    (NotificationCase.DEADLINE_REMINDER, "Deadline Reminder"),
]
_ALL_METHODS: list[tuple[NotificationMethod, str]] = [
    (NotificationMethod.EMAIL, "Email"),
]


@router.get("/settings", response_class=HTMLResponse)
async def notification_preferences_page(
    request: Request,
    db: DBSession,
    current_user: StudentUser,
    student_id: StudentId,
) -> HTMLResponse:
    result = await db.execute(
        select(NotificationPreference).where(NotificationPreference.student_id == student_id)
    )
    prefs_rows = result.scalars().all()
    prefs_map = {(r.case, r.method): r.enabled for r in prefs_rows}

    preferences = []
    for case, case_label in _ALL_CASES:
        methods = []
        for method, method_label in _ALL_METHODS:
            enabled = prefs_map.get((case, method), True)
            methods.append({"method": method, "method_label": method_label, "enabled": enabled})
        preferences.append({"case": case, "case_label": case_label, "methods": methods})

    return render(
        request, "student_settings.html", {"current_user": current_user, "preferences": preferences}
    )


@router.post("/notification-preferences/{case}/{method}/toggle")
async def toggle_notification_preference(
    case: str,
    method: str,
    db: DBSession,
    current_user: StudentUser,
    student_id: StudentId,
) -> RedirectResponse:
    result = await db.execute(
        select(NotificationPreference).where(
            NotificationPreference.student_id == student_id,
            NotificationPreference.case == case,
            NotificationPreference.method == method,
        )
    )
    pref = result.scalar_one_or_none()
    if pref is None:
        db.add(
            NotificationPreference(student_id=student_id, case=case, method=method, enabled=False)
        )
    else:
        pref.enabled = not pref.enabled
    await db.commit()
    return RedirectResponse(url="/portal/settings", status_code=303)
