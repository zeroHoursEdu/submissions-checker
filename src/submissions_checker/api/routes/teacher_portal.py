"""Teacher-facing portal routes."""

from __future__ import annotations

import csv
import io
import secrets
import urllib.parse
from datetime import UTC, date, datetime

import bcrypt
from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import and_, false, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from submissions_checker.api.dependencies import AppSettings, DBSession, TeacherUser
from submissions_checker.core.state_machine import transition
from submissions_checker.db.models import (
    FeedbackRequest,
    FeedbackResponse,
    FeedbackToken,
    OutboxMessage,
    QuizAttempt,
    Semester,
    Student,
    StudentAssignment,
    Subject,
    SubjectsAssignment,
    SubjectsStudents,
    Submission,
    User,
    UserLogin,
)
from submissions_checker.db.models.enums import OutboxEventType, OutboxMessageState, SubmissionStatus, SubjectStatus, UserRole
from submissions_checker.db.models.group import Group
from submissions_checker.services.audit import audit
from submissions_checker.services.config_apply import ConfigApplyService
from submissions_checker.services.storage import StorageService
from submissions_checker.core.templates import render

router = APIRouter(prefix="/teacher", tags=["teacher-portal"])

_SAMPLE_CSV = "student_group,student_name,student_surname,email\nIT-21,Ivan,Petrenko,ivan@example.com\nIT-21,Olena,Kovalenko,olena@example.com\n"


def _generate_password() -> str:
    return secrets.token_urlsafe(9)


async def _generate_username(base: str, db: DBSession) -> str:  # type: ignore[valid-type]
    """Return base username if available, else base_2, base_3, …"""
    candidate = base
    suffix = 2
    while True:
        result = await db.execute(select(User.id).where(User.username == candidate))
        if result.scalar_one_or_none() is None:
            return candidate
        candidate = f"{base}_{suffix}"
        suffix += 1


@router.get("", response_class=HTMLResponse)
async def teacher_dashboard(
    request: Request, db: DBSession, current_user: TeacherUser
) -> HTMLResponse:
    result = await db.execute(
        select(
            Subject.id,
            Subject.name,
            Subject.description,
            Subject.owner_id,
            func.count(SubjectsStudents.student_id).label("enrolled_count"),
        )
        .outerjoin(SubjectsStudents, SubjectsStudents.subject_id == Subject.id)
        .where(Subject.status == SubjectStatus.ACTIVE)
        .group_by(Subject.id, Subject.name, Subject.description, Subject.owner_id)
        .order_by(Subject.name)
    )
    subjects = [row._asdict() for row in result]

    apply_result = request.query_params.get("apply_result")
    apply_error = request.query_params.get("apply_error")
    if apply_error:
        apply_error = urllib.parse.unquote(apply_error)

    return render(request, "teacher_dashboard.html", {
        "current_user": current_user,
        "subjects": subjects,
        "apply_result": apply_result,
        "apply_error": apply_error,
    })


@router.post("/subjects/apply-config")
async def apply_subject_config(
    request: Request,
    db: DBSession,
    current_user: TeacherUser,
    settings: AppSettings,
    config_zip: UploadFile,
) -> RedirectResponse:
    storage = StorageService(settings) if settings.s3_endpoint_url else None
    service = ConfigApplyService(storage)
    try:
        zip_bytes = await config_zip.read()
        result = await service.apply(zip_bytes, owner_id=current_user.user_id, db=db)
        return RedirectResponse(
            f"/teacher?apply_result={result.subject_action}",
            status_code=303,
        )
    except PermissionError as exc:
        encoded = urllib.parse.quote(str(exc))
        return RedirectResponse(f"/teacher?apply_error={encoded}", status_code=303)
    except ValueError as exc:
        encoded = urllib.parse.quote(str(exc))
        return RedirectResponse(f"/teacher?apply_error={encoded}", status_code=303)
    except Exception as exc:
        from submissions_checker.core.logging import get_logger
        get_logger(__name__).error("config_apply_unexpected_error", error=str(exc))
        encoded = urllib.parse.quote("An unexpected error occurred while applying the config.")
        return RedirectResponse(f"/teacher?apply_error={encoded}", status_code=303)


@router.post("/subjects/{subject_id}/delete")
async def delete_subject(
    subject_id: int,
    db: DBSession,
    current_user: TeacherUser,
) -> RedirectResponse:
    subject = await db.get(Subject, subject_id)
    if subject is None:
        raise HTTPException(status_code=404, detail="Subject not found")
    if subject.owner_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="Only the subject owner can delete this subject")
    subject.status = SubjectStatus.DELETED
    await db.commit()
    return RedirectResponse("/teacher", status_code=303)


@router.get("/subjects/{subject_id}", response_class=HTMLResponse)
async def teacher_subject(
    request: Request, subject_id: int, db: DBSession, current_user: TeacherUser
) -> HTMLResponse:
    subject = await db.get(Subject, subject_id)
    if subject is None:
        raise HTTPException(status_code=404, detail="Subject not found")

    students_result = await db.execute(
        select(Student, Group.name.label("group_name"))
        .join(SubjectsStudents, SubjectsStudents.student_id == Student.id)
        .join(Group, Group.id == Student.group_id)
        .where(SubjectsStudents.subject_id == subject_id)
        .order_by(Group.name, Student.full_name)
    )
    students = [{"student": row.Student, "group_name": row.group_name} for row in students_result]

    assignments_result = await db.execute(
        select(SubjectsAssignment)
        .where(SubjectsAssignment.subject_id == subject_id)
        .order_by(SubjectsAssignment.deadline.asc().nullslast())
    )
    assignments = assignments_result.scalars().all()

    semester_result = await db.execute(_current_semester_query())
    current_semester = semester_result.scalar_one_or_none()

    feedback_request = None
    if current_semester:
        fr_result = await db.execute(
            select(FeedbackRequest).where(
                FeedbackRequest.subject_id == subject_id,
                FeedbackRequest.semester_id == current_semester.id,
            )
        )
        feedback_request = fr_result.scalar_one_or_none()

    feedback_sent = request.query_params.get("feedback_sent") == "1"
    feedback_error = request.query_params.get("feedback_error")

    return render(request, "teacher_subject.html", {
            "current_user": current_user,
            "subject": subject,
            "students": students,
            "assignments": assignments,
            "current_semester": current_semester,
            "feedback_request": feedback_request,
            "feedback_sent": feedback_sent,
            "feedback_error": feedback_error,
        })


@router.get("/subjects/{subject_id}/assignments/{sa_id}", response_class=HTMLResponse)
async def teacher_assignment(
    request: Request,
    subject_id: int,
    sa_id: int,
    db: DBSession,
    current_user: TeacherUser,
) -> HTMLResponse:
    assignment_result = await db.execute(
        select(SubjectsAssignment)
        .where(
            SubjectsAssignment.id == sa_id,
            SubjectsAssignment.subject_id == subject_id,
        )
        .options(selectinload(SubjectsAssignment.subject))
    )
    assignment = assignment_result.scalar_one_or_none()
    if assignment is None:
        raise HTTPException(status_code=404, detail="Assignment not found")

    # Latest submission per student_assignment
    latest_sub_sq = (
        select(
            Submission.students_assignment_id,
            func.max(Submission.created_at).label("max_created_at"),
        )
        .group_by(Submission.students_assignment_id)
        .subquery()
    )

    rows_result = await db.execute(
        select(
            Student.full_name,
            Student.github_username,
            StudentAssignment.id.label("student_assignment_id"),
            StudentAssignment.grade,
            Submission.id.label("submission_id"),
            Submission.status.label("submission_status"),
            Submission.created_at.label("submitted_at"),
            Submission.source_metadata.label("source_metadata"),
        )
        .select_from(SubjectsStudents)
        .join(Student, Student.id == SubjectsStudents.student_id)
        .outerjoin(
            StudentAssignment,
            and_(
                StudentAssignment.student_id == SubjectsStudents.student_id,
                StudentAssignment.subjects_assignment_id == sa_id,
            ),
        )
        .outerjoin(
            latest_sub_sq,
            latest_sub_sq.c.students_assignment_id == StudentAssignment.id,
        )
        .outerjoin(
            Submission,
            and_(
                Submission.students_assignment_id == StudentAssignment.id,
                Submission.created_at == latest_sub_sq.c.max_created_at,
            ),
        )
        .where(SubjectsStudents.subject_id == subject_id)
        .order_by(Student.full_name)
    )
    rows = [row._asdict() for row in rows_result]

    # Load violation flags: for each student_assignment, find if any attempt has violations
    sa_id_list = [r["student_assignment_id"] for r in rows if r["student_assignment_id"]]
    violation_flags: dict[int, dict] = {}
    if sa_id_list:
        viol_result = await db.execute(
            select(
                Submission.students_assignment_id,
                QuizAttempt.violations,
            )
            .join(Submission, Submission.id == QuizAttempt.submission_id)
            .where(
                Submission.students_assignment_id.in_(sa_id_list),
                func.jsonb_object_length(QuizAttempt.violations) > 0,
            )
            .order_by(QuizAttempt.started_at.desc())
        )
        for vr in viol_result:
            sa_id_val = vr.students_assignment_id
            if sa_id_val not in violation_flags:
                violation_flags[sa_id_val] = vr.violations or {}

    return render(request, "teacher_assignment.html", {
            "current_user": current_user,
            "assignment": assignment,
            "subject_id": subject_id,
            "rows": rows,
            "violation_flags": violation_flags,
        })


@router.get("/students/sample.csv")
async def download_sample_csv(current_user: TeacherUser) -> StreamingResponse:
    """Return a sample CSV template for teachers to share with their students."""
    return StreamingResponse(
        iter([_SAMPLE_CSV]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=students.csv"},
    )


@router.get("/subjects/{subject_id}/students/template.csv")
async def download_subject_enrollment_template(
    subject_id: int,
    db: DBSession,
    current_user: TeacherUser,
) -> StreamingResponse:
    """Generate enrollment+variant CSV template for a subject.

    Columns: student_group, student_name, student_surname, email
    + one variant_{assignment.code} column per assignment with variants_required=true.
    """
    subject = await db.get(Subject, subject_id)
    if subject is None:
        raise HTTPException(status_code=404, detail="Subject not found")

    assignments_result = await db.execute(
        select(SubjectsAssignment)
        .where(SubjectsAssignment.subject_id == subject_id)
        .order_by(SubjectsAssignment.title)
    )
    assignments = assignments_result.scalars().all()

    variant_columns = [
        f"variant_{sa.code}"
        for sa in assignments
        if sa.code and sa.config.get("variants_required")
    ]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["student_group", "student_name", "student_surname", "email"] + variant_columns)

    # Include currently enrolled students as example rows
    students_result = await db.execute(
        select(Student, Group.name.label("group_name"))
        .join(SubjectsStudents, SubjectsStudents.student_id == Student.id)
        .join(Group, Group.id == Student.group_id)
        .where(SubjectsStudents.subject_id == subject_id)
        .order_by(Student.full_name)
    )
    for row in students_result:
        student = row.Student
        name_parts = student.full_name.split(" ", 1)
        first = name_parts[0] if name_parts else ""
        last = name_parts[1] if len(name_parts) > 1 else ""
        writer.writerow([row.group_name, first, last, student.email] + [""] * len(variant_columns))

    csv_content = output.getvalue()
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=subject_{subject_id}_students.csv"},
    )


@router.post("/subjects/{subject_id}/students/import")
async def import_subject_students(
    subject_id: int,
    db: DBSession,
    current_user: TeacherUser,
    file: UploadFile,
) -> RedirectResponse:
    """Import enrollment+variant CSV for a specific subject.

    Creates/enrolls students and sets per-assignment variants from variant_ columns.
    """
    subject = await db.get(Subject, subject_id)
    if subject is None:
        raise HTTPException(status_code=404, detail="Subject not found")

    if file.size and file.size > 1_048_576:
        raise HTTPException(status_code=413, detail="File too large (max 1 MB)")

    content = await file.read()
    try:
        text_content = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="File must be UTF-8 encoded")

    reader = csv.DictReader(io.StringIO(text_content))
    required = {"student_group", "student_name", "student_surname", "email"}
    fieldnames = set(reader.fieldnames or [])
    if not required.issubset(fieldnames):
        missing = required - fieldnames
        raise HTTPException(status_code=422, detail=f"Missing CSV columns: {', '.join(sorted(missing))}")

    # Identify variant columns and their assignment codes
    variant_col_map: dict[str, str] = {}  # col_name → assignment_code
    for col in fieldnames:
        if col.startswith("variant_"):
            variant_col_map[col] = col[len("variant_"):]

    # Load subject assignments by code for variant validation
    assignments_by_code: dict[str, SubjectsAssignment] = {}
    if variant_col_map:
        sa_result = await db.execute(
            select(SubjectsAssignment).where(SubjectsAssignment.subject_id == subject_id)
        )
        for sa in sa_result.scalars():
            if sa.code:
                assignments_by_code[sa.code] = sa

    imported_count = 0
    skipped_count = 0
    variants_updated = 0

    for row in reader:
        group_name = row["student_group"].strip()
        first_name = row["student_name"].strip()
        last_name = row["student_surname"].strip()
        email = row["email"].strip().lower()

        if not all([group_name, first_name, last_name, email]):
            continue

        # Get or create student
        existing_result = await db.execute(select(Student).where(Student.email == email))
        student = existing_result.scalar_one_or_none()

        if student is None:
            group_result = await db.execute(select(Group).where(Group.name == group_name))
            group = group_result.scalar_one_or_none()
            if group is None:
                group = Group(name=group_name)
                db.add(group)
                await db.flush()

            full_name = f"{first_name} {last_name}"
            student = Student(group_id=group.id, full_name=full_name, email=email)
            db.add(student)
            await db.flush()

            base_username = f"{first_name.lower()}.{last_name.lower()}"
            username = await _generate_username(base_username, db)
            password = _generate_password()
            password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode()

            user = User(
                username=username,
                password_hash=password_hash,
                role=UserRole.STUDENT,
                student_id=student.id,
            )
            db.add(user)
            await db.flush()

            db.add(OutboxMessage(
                event_type=OutboxEventType.SEND_CREDENTIALS,
                state=OutboxMessageState.PENDING,
                payload={
                    "student_email": email,
                    "full_name": full_name,
                    "username": username,
                    "password": password,
                },
            ))
            imported_count += 1
        else:
            skipped_count += 1

        # Enroll in subject if not already enrolled
        enrollment_result = await db.execute(
            select(SubjectsStudents).where(
                SubjectsStudents.subject_id == subject_id,
                SubjectsStudents.student_id == student.id,
            )
        )
        if enrollment_result.scalar_one_or_none() is None:
            db.add(SubjectsStudents(subject_id=subject_id, student_id=student.id))
            sa_ids_result = await db.execute(
                select(SubjectsAssignment.id).where(SubjectsAssignment.subject_id == subject_id)
            )
            for (sa_id_val,) in sa_ids_result:
                existing_sa = await db.execute(
                    select(StudentAssignment.id).where(
                        StudentAssignment.student_id == student.id,
                        StudentAssignment.subjects_assignment_id == sa_id_val,
                    )
                )
                if existing_sa.scalar_one_or_none() is None:
                    db.add(StudentAssignment(student_id=student.id, subjects_assignment_id=sa_id_val))
            await db.flush()

        # Set variants from variant_ columns
        for col_name, assignment_code in variant_col_map.items():
            variant_value = row.get(col_name, "").strip()
            if not variant_value:
                continue
            sa = assignments_by_code.get(assignment_code)
            if sa is None:
                continue
            sa_result = await db.execute(
                select(StudentAssignment).where(
                    StudentAssignment.student_id == student.id,
                    StudentAssignment.subjects_assignment_id == sa.id,
                )
            )
            student_assignment = sa_result.scalar_one_or_none()
            if student_assignment is not None:
                student_assignment.variant = variant_value
                variants_updated += 1

    await db.commit()

    return RedirectResponse(
        url=f"/teacher/subjects/{subject_id}?imported={imported_count}&skipped={skipped_count}&variants_updated={variants_updated}",
        status_code=303,
    )


@router.get("/students", response_class=HTMLResponse)
async def teacher_students(
    request: Request,
    db: DBSession,
    current_user: TeacherUser,
    imported: int = 0,
    skipped: int = 0,
) -> HTMLResponse:
    """Student registration overview: list all students with account/email/login status."""
    rows_result = await db.execute(
        select(
            Student.id,
            Student.full_name,
            Student.email,
            Group.name.label("group_name"),
            User.username,
            User.is_active,
            OutboxMessage.state.label("email_state"),
            func.min(UserLogin.logged_in_at).label("first_login"),
        )
        .join(Group, Group.id == Student.group_id)
        .outerjoin(User, User.student_id == Student.id)
        .outerjoin(
            OutboxMessage,
            and_(
                OutboxMessage.event_type == OutboxEventType.SEND_CREDENTIALS,
                text("outbox_messages.payload->>'student_email' = students.email"),
            ),
        )
        .outerjoin(UserLogin, UserLogin.user_id == User.id)
        .group_by(Student.id, Group.name, User.username, User.is_active, OutboxMessage.state)
        .order_by(Student.created_at.desc())
    )
    students = [row._asdict() for row in rows_result]

    return render(request, "teacher_students.html", {
            "current_user": current_user,
            "students": students,
            "imported": imported,
            "skipped": skipped,
        })


@router.post("/students/import")
async def import_students(
    request: Request,
    db: DBSession,
    current_user: TeacherUser,
    file: UploadFile,
) -> RedirectResponse:
    """Parse uploaded CSV and create student accounts + credential outbox tasks."""
    if file.size and file.size > 1_048_576:
        raise HTTPException(status_code=413, detail="File too large (max 1 MB)")

    content = await file.read()
    try:
        text_content = content.decode("utf-8-sig")  # strips BOM if present
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="File must be UTF-8 encoded")

    reader = csv.DictReader(io.StringIO(text_content))
    required = {"student_group", "student_name", "student_surname", "email"}
    if not required.issubset(set(reader.fieldnames or [])):
        missing = required - set(reader.fieldnames or [])
        raise HTTPException(status_code=422, detail=f"Missing CSV columns: {', '.join(sorted(missing))}")

    imported_count = 0
    skipped_count = 0

    for row in reader:
        group_name = row["student_group"].strip()
        first_name = row["student_name"].strip()
        last_name = row["student_surname"].strip()
        email = row["email"].strip().lower()

        if not all([group_name, first_name, last_name, email]):
            continue

        # Skip if student already registered
        existing = await db.execute(select(Student.id).where(Student.email == email))
        if existing.scalar_one_or_none() is not None:
            skipped_count += 1
            continue

        # Get or create group
        group_result = await db.execute(select(Group).where(Group.name == group_name))
        group = group_result.scalar_one_or_none()
        if group is None:
            group = Group(name=group_name)
            db.add(group)
            await db.flush()

        # Create student
        full_name = f"{first_name} {last_name}"
        student = Student(group_id=group.id, full_name=full_name, email=email)
        db.add(student)
        await db.flush()

        # Generate unique username and password
        base_username = f"{first_name.lower()}.{last_name.lower()}"
        username = await _generate_username(base_username, db)
        password = _generate_password()
        password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode()

        # Create user account
        user = User(
            username=username,
            password_hash=password_hash,
            role=UserRole.STUDENT,
            student_id=student.id,
        )
        db.add(user)
        await db.flush()

        # Queue credentials email
        outbox = OutboxMessage(
            event_type=OutboxEventType.SEND_CREDENTIALS,
            state=OutboxMessageState.PENDING,
            payload={
                "student_email": email,
                "full_name": full_name,
                "username": username,
                "password": password,
            },
        )
        db.add(outbox)
        imported_count += 1

    await db.commit()

    return RedirectResponse(
        url=f"/teacher/students?imported={imported_count}&skipped={skipped_count}",
        status_code=303,
    )


@router.get("/submissions/{submission_id}/review", response_class=HTMLResponse)
async def teacher_review_submission(
    request: Request,
    submission_id: int,
    db: DBSession,
    current_user: TeacherUser,
) -> HTMLResponse:
    result = await db.execute(
        select(Submission)
        .where(Submission.id == submission_id)
        .options(
            selectinload(Submission.students_assignment).selectinload(
                StudentAssignment.subjects_assignment
            ).selectinload(SubjectsAssignment.subject),
            selectinload(Submission.students_assignment).selectinload(
                StudentAssignment.student
            ),
        )
    )
    submission = result.scalar_one_or_none()
    _teacher_review_statuses = {
        SubmissionStatus.WAITING_FOR_TEACHER_REVIEW,
        SubmissionStatus.AWAITING_TEACHER_REVIEW,
    }
    if submission is None or submission.status not in _teacher_review_statuses:
        raise HTTPException(status_code=404)

    sa = submission.students_assignment
    subjects_assignment = sa.subjects_assignment

    return render(request, "teacher_submission_review.html", {
            "current_user": current_user,
            "submission": submission,
            "student": sa.student,
            "assignment": subjects_assignment,
            "subject": subjects_assignment.subject,
        })


@router.post("/submissions/{submission_id}/review")
async def teacher_review_submission_action(
    submission_id: int,
    db: DBSession,
    current_user: TeacherUser,
    action: str = Form(...),
    reason: str = Form(""),
) -> RedirectResponse:
    result = await db.execute(
        select(Submission)
        .where(Submission.id == submission_id)
        .options(
            selectinload(Submission.students_assignment).selectinload(
                StudentAssignment.subjects_assignment
            ),
            selectinload(Submission.plugin_config),
        )
    )
    submission = result.scalar_one_or_none()
    _teacher_review_statuses = {
        SubmissionStatus.WAITING_FOR_TEACHER_REVIEW,
        SubmissionStatus.AWAITING_TEACHER_REVIEW,
    }
    if submission is None or submission.status not in _teacher_review_statuses:
        raise HTTPException(status_code=404)

    sa = submission.students_assignment
    subjects_assignment = sa.subjects_assignment

    clean_reason = reason.strip()
    if action == "approve":
        has_quiz = False
        if submission.plugin_config and subjects_assignment.code:
            asgn_cfg = (
                submission.plugin_config.config
                .get("assignments", {})
                .get(subjects_assignment.code, {})
            )
            has_quiz = bool(asgn_cfg.get("quiz", {}).get("questions"))
        if submission.status == SubmissionStatus.AWAITING_TEACHER_REVIEW:
            transition(submission, "teacher_send_quiz" if has_quiz else "teacher_approve")
        else:
            transition(submission, "teacher_approve_quiz" if has_quiz else "teacher_approve_done")
    elif action == "reject":
        submission.test_results = {"check_reason": clean_reason or "Rejected by teacher"}
        if submission.status == SubmissionStatus.AWAITING_TEACHER_REVIEW:
            transition(submission, "teacher_reject")
        else:
            transition(submission, "teacher_reject")
    else:
        raise HTTPException(status_code=400, detail="Invalid action")

    # Queue email notification to student
    db.add(OutboxMessage(
        event_type=OutboxEventType.SUBMISSION_REVIEWED,
        state=OutboxMessageState.PENDING,
        payload={
            "submission_id": submission_id,
            "action": action,
            "reason": clean_reason,
        },
    ))

    await audit(
        db,
        action=f"teacher_{action}_submission",
        actor_id=current_user.user_id,
        actor_username=current_user.username,
        target_type="submission",
        target_id=submission_id,
        reason=clean_reason,
    )

    await db.commit()

    return RedirectResponse(
        url=f"/teacher/subjects/{subjects_assignment.subject_id}/assignments/{subjects_assignment.id}",
        status_code=303,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Student enrollment management
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/subjects/{subject_id}/enroll/{student_id_param}")
async def enroll_student(
    subject_id: int,
    student_id_param: int,
    db: DBSession,
    current_user: TeacherUser,
) -> RedirectResponse:
    subject = await db.get(Subject, subject_id)
    if subject is None:
        raise HTTPException(status_code=404)
    existing = await db.execute(
        select(SubjectsStudents).where(
            SubjectsStudents.subject_id == subject_id,
            SubjectsStudents.student_id == student_id_param,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(SubjectsStudents(subject_id=subject_id, student_id=student_id_param))
        # Create StudentAssignment records for all existing assignments in this subject
        assignments_result = await db.execute(
            select(SubjectsAssignment.id).where(SubjectsAssignment.subject_id == subject_id)
        )
        for (sa_id,) in assignments_result:
            # Only if not already exists
            existing_sa = await db.execute(
                select(StudentAssignment.id).where(
                    StudentAssignment.student_id == student_id_param,
                    StudentAssignment.subjects_assignment_id == sa_id,
                )
            )
            if existing_sa.scalar_one_or_none() is None:
                db.add(StudentAssignment(student_id=student_id_param, subjects_assignment_id=sa_id))
        await audit(
            db, action="enroll_student", actor_id=current_user.user_id,
            actor_username=current_user.username, subject_id=subject_id, student_id=student_id_param,
        )
        await db.commit()
    return RedirectResponse(url=f"/teacher/subjects/{subject_id}", status_code=303)


@router.post("/subjects/{subject_id}/unenroll/{student_id_param}")
async def unenroll_student(
    subject_id: int,
    student_id_param: int,
    db: DBSession,
    current_user: TeacherUser,
) -> RedirectResponse:
    result = await db.execute(
        select(SubjectsStudents).where(
            SubjectsStudents.subject_id == subject_id,
            SubjectsStudents.student_id == student_id_param,
        )
    )
    enrollment = result.scalar_one_or_none()
    if enrollment:
        await db.delete(enrollment)
        await audit(
            db, action="unenroll_student", actor_id=current_user.user_id,
            actor_username=current_user.username, subject_id=subject_id, student_id=student_id_param,
        )
        await db.commit()
    return RedirectResponse(url=f"/teacher/subjects/{subject_id}", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# Grade export (CSV)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/subjects/{subject_id}/export.csv")
async def export_grades_csv(
    subject_id: int,
    db: DBSession,
    current_user: TeacherUser,
) -> StreamingResponse:
    """Download all grades for a subject as CSV."""
    subject = await db.get(Subject, subject_id)
    if subject is None:
        raise HTTPException(status_code=404, detail="Subject not found")

    result = await db.execute(
        select(
            Student.full_name,
            Student.email,
            Group.name.label("group_name"),
            SubjectsAssignment.title.label("assignment_title"),
            SubjectsAssignment.max_grade,
            StudentAssignment.grade,
            Submission.status.label("submission_status"),
            Submission.created_at.label("submitted_at"),
        )
        .select_from(SubjectsStudents)
        .join(Student, Student.id == SubjectsStudents.student_id)
        .join(Group, Group.id == Student.group_id)
        .join(SubjectsAssignment, SubjectsAssignment.subject_id == subject_id)
        .outerjoin(
            StudentAssignment,
            and_(
                StudentAssignment.student_id == SubjectsStudents.student_id,
                StudentAssignment.subjects_assignment_id == SubjectsAssignment.id,
            ),
        )
        .outerjoin(
            Submission,
            and_(
                Submission.students_assignment_id == StudentAssignment.id,
                Submission.created_at == select(func.max(Submission.created_at))
                .where(Submission.students_assignment_id == StudentAssignment.id)
                .scalar_subquery(),
            ),
        )
        .where(SubjectsStudents.subject_id == subject_id)
        .order_by(Group.name, Student.full_name, SubjectsAssignment.title)
    )
    rows = result.all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Student", "Email", "Group", "Assignment", "Grade", "Max Grade", "Status", "Submitted At"])
    for r in rows:
        writer.writerow([
            r.full_name,
            r.email,
            r.group_name,
            r.assignment_title,
            r.grade if r.grade is not None else "",
            r.max_grade,
            r.submission_status or "",
            r.submitted_at.strftime("%Y-%m-%d %H:%M") if r.submitted_at else "",
        ])
    output.seek(0)

    filename = f"{subject.name.replace(' ', '_')}_grades.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Add individual student
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/students/add", response_class=HTMLResponse)
async def add_student_page(
    request: Request,
    db: DBSession,
    current_user: TeacherUser,
) -> HTMLResponse:
    groups_result = await db.execute(select(Group).order_by(Group.name))
    groups = groups_result.scalars().all()
    return render(request, "teacher_add_student.html", {"current_user": current_user, "groups": groups, "error": None, "success": None})


@router.post("/students/add", response_model=None)
async def add_student(
    request: Request,
    db: DBSession,
    current_user: TeacherUser,
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(...),
    group_name: str = Form(...),
    github_username: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    email = email.strip().lower()
    group_name = group_name.strip()

    existing = await db.execute(select(Student.id).where(Student.email == email))
    if existing.scalar_one_or_none() is not None:
        groups_result = await db.execute(select(Group).order_by(Group.name))
        return render(request, "teacher_add_student.html", {
                "current_user": current_user,
                "groups": groups_result.scalars().all(),
                "error": "A student with this email already exists.",
                "success": None,
            }, status_code=422)  # type: ignore[return-value]

    group_result = await db.execute(select(Group).where(Group.name == group_name))
    group = group_result.scalar_one_or_none()
    if group is None:
        group = Group(name=group_name)
        db.add(group)
        await db.flush()

    full_name = f"{first_name.strip()} {last_name.strip()}"
    student = Student(
        group_id=group.id,
        full_name=full_name,
        email=email,
        github_username=github_username.strip() or None,
    )
    db.add(student)
    await db.flush()

    base_username = f"{first_name.strip().lower()}.{last_name.strip().lower()}"
    username = await _generate_username(base_username, db)
    password = _generate_password()
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode()

    user = User(
        username=username,
        password_hash=password_hash,
        role=UserRole.STUDENT,
        student_id=student.id,
    )
    db.add(user)
    await db.flush()

    db.add(OutboxMessage(
        event_type=OutboxEventType.SEND_CREDENTIALS,
        state=OutboxMessageState.PENDING,
        payload={
            "student_email": email,
            "full_name": full_name,
            "username": username,
            "password": password,
        },
    ))

    await audit(
        db, action="add_student", actor_id=current_user.user_id,
        actor_username=current_user.username, student_email=email,
    )
    await db.commit()

    return RedirectResponse(url="/teacher/students?imported=1&skipped=0", status_code=303)


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

def _current_semester_query():
    today = date.today()
    return select(Semester).where(Semester.start_date <= today, Semester.end_date >= today)


@router.post("/subjects/{subject_id}/feedback/request")
async def request_feedback(
    subject_id: int,
    request: Request,
    db: DBSession,
    current_user: TeacherUser,
) -> RedirectResponse:
    subject = await db.get(Subject, subject_id)
    if subject is None:
        raise HTTPException(status_code=404, detail="Subject not found")

    semester_result = await db.execute(_current_semester_query())
    semester = semester_result.scalar_one_or_none()
    if semester is None:
        return RedirectResponse(
            url=f"/teacher/subjects/{subject_id}?feedback_error=no_active_semester",
            status_code=303,
        )

    students_result = await db.execute(
        select(Student)
        .join(SubjectsStudents, SubjectsStudents.student_id == Student.id)
        .where(SubjectsStudents.subject_id == subject_id)
    )
    students = students_result.scalars().all()

    feedback_request = FeedbackRequest(
        subject_id=subject_id,
        semester_id=semester.id,
        created_by_teacher_id=current_user.user_id,
    )
    db.add(feedback_request)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return RedirectResponse(
            url=f"/teacher/subjects/{subject_id}?feedback_error=already_sent",
            status_code=303,
        )

    for student in students:
        token_str = secrets.token_urlsafe(32)
        db.add(FeedbackToken(
            feedback_request_id=feedback_request.id,
            student_id=student.id,
            token=token_str,
        ))
        await db.flush()
        token_result = await db.execute(
            select(FeedbackToken).where(FeedbackToken.token == token_str)
        )
        saved_token = token_result.scalar_one()
        db.add(OutboxMessage(
            event_type=OutboxEventType.FEEDBACK_REQUEST_SENT,
            state=OutboxMessageState.PENDING,
            payload={"feedback_token_id": saved_token.id},
        ))

    await db.commit()
    return RedirectResponse(
        url=f"/teacher/subjects/{subject_id}?feedback_sent=1",
        status_code=303,
    )


@router.get("/subjects/{subject_id}/feedback", response_class=HTMLResponse)
async def view_feedback(
    subject_id: int,
    request: Request,
    db: DBSession,
    current_user: TeacherUser,
) -> HTMLResponse:
    subject = await db.get(Subject, subject_id)
    if subject is None:
        raise HTTPException(status_code=404, detail="Subject not found")

    semester_result = await db.execute(_current_semester_query())
    current_semester = semester_result.scalar_one_or_none()

    fr_result = await db.execute(
        select(FeedbackRequest)
        .where(
            FeedbackRequest.subject_id == subject_id,
            *(
                [FeedbackRequest.semester_id == current_semester.id]
                if current_semester
                else [false()]
            ),
        )
        .options(selectinload(FeedbackRequest.semester))
    )
    feedback_request = fr_result.scalar_one_or_none()

    responses = []
    avg_rating = None
    if feedback_request:
        rows_result = await db.execute(
            select(FeedbackResponse, Student)
            .join(FeedbackToken, FeedbackToken.id == FeedbackResponse.feedback_token_id)
            .join(Student, Student.id == FeedbackToken.student_id)
            .where(FeedbackResponse.subject_id == subject_id)
            .order_by(FeedbackResponse.submitted_at.desc())
        )
        rows = rows_result.all()
        responses = [{"response": r, "student": s} for r, s in rows]
        if responses:
            avg_rating = round(sum(row["response"].rating for row in responses) / len(responses), 1)

    return render(request, "teacher_feedback_view.html", {
            "current_user": current_user,
            "subject": subject,
            "feedback_request": feedback_request,
            "responses": responses,
            "avg_rating": avg_rating,
            "current_semester": current_semester,
        })


@router.get("/subjects/{subject_id}/feedback/export.csv")
async def export_feedback_csv(
    subject_id: int,
    db: DBSession,
    current_user: TeacherUser,
) -> StreamingResponse:
    subject = await db.get(Subject, subject_id)
    if subject is None:
        raise HTTPException(status_code=404, detail="Subject not found")

    rows_result = await db.execute(
        select(FeedbackResponse, Student)
        .join(FeedbackToken, FeedbackToken.id == FeedbackResponse.feedback_token_id)
        .join(Student, Student.id == FeedbackToken.student_id)
        .where(FeedbackResponse.subject_id == subject_id)
        .order_by(FeedbackResponse.submitted_at.asc())
    )
    rows = rows_result.all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["student_name", "student_email", "rating", "went_well", "went_bad", "to_change", "submitted_at"])
    for resp, student in rows:
        writer.writerow([
            student.full_name,
            student.email,
            resp.rating,
            resp.went_well,
            resp.went_bad,
            resp.to_change,
            resp.submitted_at.isoformat(),
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=feedback_{subject_id}.csv"},
    )
