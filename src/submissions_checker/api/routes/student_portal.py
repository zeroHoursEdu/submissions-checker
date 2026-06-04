"""Student-facing portal routes — requires student authentication."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import aiofiles
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, nullslast, select
from sqlalchemy.orm import selectinload

from submissions_checker.api.dependencies import DBSession, StudentId, StudentUser
from submissions_checker.api.schemas.student_portal import (
    AssignmentDetail,
    AssignmentRow,
    SubjectCard,
)
from submissions_checker.core.state_machine import transition
from submissions_checker.db.models import (
    OutboxMessage,
    QuizAttempt,
    QuizTemplate,
    Student,
    StudentAssignment,
    Subject,
    SubjectsAssignment,
    SubjectsStudents,
    Submission,
    SubmissionSourceType,
    SubmissionStatus,
)
from submissions_checker.db.models.enums import OutboxEventType, OutboxMessageState, QuizAttemptStatus
from submissions_checker.services.audit import audit
from submissions_checker.services.notification_service import push_notification
from submissions_checker.services.similarity import compare_zip_files
from submissions_checker.services.submission_checker import check_submission

router = APIRouter(prefix="/portal", tags=["student-portal"])
templates = Jinja2Templates(directory="templates")

UPLOADS_DIR = Path("uploads")
UPLOADS_DIR.mkdir(exist_ok=True)


@router.get("", response_class=HTMLResponse)
async def subjects_grid(
    request: Request, db: DBSession, current_user: StudentUser, student_id: StudentId
) -> HTMLResponse:
    student = await db.get(Student, student_id)
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")

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
            .join(StudentAssignment, StudentAssignment.subjects_assignment_id == SubjectsAssignment.id)
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
        )
        for s in subjects
    ]

    return templates.TemplateResponse(
        request=request,
        name="subjects.html",
        context={"current_user": current_user, "student": student, "subjects": subject_cards},
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

    assignment_rows = []
    for sa in student_assignments:
        latest_sub = (
            max(sa.submissions, key=lambda s: s.created_at) if sa.submissions else None
        )
        assignment_rows.append(
            AssignmentRow(
                student_assignment_id=sa.id,
                title=sa.subjects_assignment.title,
                deadline=sa.subjects_assignment.deadline,
                grade=sa.grade,
                min_grade=sa.subjects_assignment.min_grade,
                max_grade=sa.subjects_assignment.max_grade,
                submission_status=latest_sub.status if latest_sub else None,
            )
        )

    return templates.TemplateResponse(
        request=request,
        name="assignments.html",
        context={
            "current_user": current_user,
            "student": student,
            "subject": subject,
            "assignments": assignment_rows,
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

    latest_sub = (
        max(sa.submissions, key=lambda s: s.created_at) if sa.submissions else None
    )

    # Quiz attempt metadata: count used attempts across all submissions for this sa
    attempts_used_result = await db.execute(
        select(func.count(QuizAttempt.id)).where(
            and_(
                QuizAttempt.submission_id.in_(
                    select(Submission.id).where(Submission.students_assignment_id == sa_id)
                ),
                QuizAttempt.status.in_([QuizAttemptStatus.COMPLETED, QuizAttemptStatus.TIMED_OUT]),
            )
        )
    )
    quiz_attempts_used: int = attempts_used_result.scalar_one() or 0

    # Latest attempt id (for result link) and max_attempts from template config
    quiz_attempt_id: int | None = None
    quiz_max_attempts: int | None = None
    if latest_sub:
        latest_attempt_result = await db.execute(
            select(QuizAttempt)
            .where(QuizAttempt.submission_id == latest_sub.id)
            .order_by(QuizAttempt.started_at.desc())
            .limit(1)
        )
        latest_attempt = latest_attempt_result.scalar_one_or_none()
        if latest_attempt:
            quiz_attempt_id = latest_attempt.id

    tmpl_result = await db.execute(
        select(QuizTemplate.config).where(
            QuizTemplate.subjects_assignment_id == sa.subjects_assignment_id
        )
    )
    tmpl_cfg = tmpl_result.scalar_one_or_none()
    if tmpl_cfg:
        quiz_max_attempts = tmpl_cfg.get("max_quiz_attempts")

    check_reason: str | None = None
    if latest_sub and latest_sub.test_results:
        check_reason = latest_sub.test_results.get("check_reason")

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
    )

    student = await db.get(Student, student_id)

    return templates.TemplateResponse(
        request=request,
        name="assignment_detail.html",
        context={
            "current_user": current_user,
            "student": student,
            "subject_id": subject_id,
            "assignment": detail,
        },
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

    # Re-submission limit
    max_submissions = subjects_assignment.config.get("max_submissions")
    if max_submissions is not None:
        count_result = await db.execute(
            select(func.count(Submission.id)).where(Submission.students_assignment_id == sa_id)
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
        select(Submission.source_metadata)
        .where(
            Submission.students_assignment_id.in_(
                select(StudentAssignment.id).where(
                    StudentAssignment.subjects_assignment_id == sa.subjects_assignment_id,
                    StudentAssignment.student_id != student_id,
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
    )
    db.add(submission)
    await db.flush()

    transition(submission, "start_check")

    passed, reason = check_submission(save_path)

    if not passed:
        submission.test_results = {"check_reason": reason}
        transition(submission, "check_failed")
    else:
        review_mode = subjects_assignment.config.get("review_mode", "none")

        if review_mode == "quiz":
            transition(submission, "check_passed_quiz")
        elif review_mode == "teacher_review":
            transition(submission, "check_passed_teacher_review")
            # Notify teachers that a new submission awaits review
            db.add(OutboxMessage(
                event_type=OutboxEventType.NEW_SUBMISSION,
                state=OutboxMessageState.PENDING,
                payload={"submission_id": submission.id},
            ))
        else:
            transition(submission, "check_passed_none")

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
            select(Submission)
            .join(latest_sub_sq, and_(
                Submission.students_assignment_id == latest_sub_sq.c.students_assignment_id,
                Submission.created_at == latest_sub_sq.c.max_at,
            ))
        )
        subs_by_sa = {s.students_assignment_id: s for s in sub_result.scalars().all()}
    else:
        subs_by_sa = {}

    # Build subject lookup
    subject_map = {s.id: s for s in subjects}

    summary_rows = []
    for r in all_rows:
        sa = r.SubjectsAssignment
        deadline = sa.deadline
        if deadline and deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        sub = subs_by_sa.get(r.student_assignment_id)
        is_overdue = deadline is not None and deadline < now and r.grade is None
        summary_rows.append({
            "subject": subject_map.get(sa.subject_id),
            "assignment": sa,
            "student_assignment_id": r.student_assignment_id,
            "grade": r.grade,
            "submission_status": sub.status if sub else None,
            "deadline": deadline,
            "is_overdue": is_overdue,
        })

    return templates.TemplateResponse(
        request=request,
        name="student_summary.html",
        context={
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
