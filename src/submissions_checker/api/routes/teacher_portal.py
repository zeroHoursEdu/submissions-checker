"""Teacher-facing portal routes."""

from __future__ import annotations

import csv
import io
import json
import secrets
from datetime import UTC, datetime

import bcrypt
from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, select, text
from sqlalchemy.orm import selectinload

from submissions_checker.api.dependencies import DBSession, TeacherUser
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
    User,
    UserLogin,
)
from submissions_checker.db.models.enums import OutboxEventType, OutboxMessageState, SubmissionStatus, UserRole
from submissions_checker.db.models.group import Group
from submissions_checker.services.audit import audit
from submissions_checker.services.notification_service import push_notification

router = APIRouter(prefix="/teacher", tags=["teacher-portal"])
templates = Jinja2Templates(directory="templates")

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
            func.count(SubjectsStudents.student_id).label("enrolled_count"),
        )
        .outerjoin(SubjectsStudents, SubjectsStudents.subject_id == Subject.id)
        .group_by(Subject.id, Subject.name, Subject.description)
        .order_by(Subject.name)
    )
    subjects = [row._asdict() for row in result]

    return templates.TemplateResponse(
        request=request,
        name="teacher_dashboard.html",
        context={"current_user": current_user, "subjects": subjects},
    )


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

    return templates.TemplateResponse(
        request=request,
        name="teacher_subject.html",
        context={
            "current_user": current_user,
            "subject": subject,
            "students": students,
            "assignments": assignments,
        },
    )


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

    return templates.TemplateResponse(
        request=request,
        name="teacher_assignment.html",
        context={
            "current_user": current_user,
            "assignment": assignment,
            "subject_id": subject_id,
            "rows": rows,
            "violation_flags": violation_flags,
        },
    )


@router.get("/students/sample.csv")
async def download_sample_csv(current_user: TeacherUser) -> StreamingResponse:
    """Return a sample CSV template for teachers to share with their students."""
    return StreamingResponse(
        iter([_SAMPLE_CSV]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=students.csv"},
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

    return templates.TemplateResponse(
        request=request,
        name="teacher_students.html",
        context={
            "current_user": current_user,
            "students": students,
            "imported": imported,
            "skipped": skipped,
        },
    )


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
    if submission is None or submission.status != SubmissionStatus.WAITING_FOR_TEACHER_REVIEW:
        raise HTTPException(status_code=404)

    sa = submission.students_assignment
    subjects_assignment = sa.subjects_assignment

    return templates.TemplateResponse(
        request=request,
        name="teacher_submission_review.html",
        context={
            "current_user": current_user,
            "submission": submission,
            "student": sa.student,
            "assignment": subjects_assignment,
            "subject": subjects_assignment.subject,
        },
    )


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
            ).selectinload(SubjectsAssignment.quiz_template),
        )
    )
    submission = result.scalar_one_or_none()
    if submission is None or submission.status != SubmissionStatus.WAITING_FOR_TEACHER_REVIEW:
        raise HTTPException(status_code=404)

    sa = submission.students_assignment
    subjects_assignment = sa.subjects_assignment

    clean_reason = reason.strip()
    if action == "approve":
        has_quiz = subjects_assignment.quiz_template is not None
        transition(submission, "teacher_approve_quiz" if has_quiz else "teacher_approve_done")
    elif action == "reject":
        submission.test_results = {"check_reason": clean_reason or "Rejected by teacher"}
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
# Subject CRUD
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/subjects/create", response_class=HTMLResponse)
async def create_subject_page(
    request: Request,
    current_user: TeacherUser,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="teacher_subject_form.html",
        context={"current_user": current_user, "subject": None, "error": None},
    )


@router.post("/subjects/create", response_model=None)
async def create_subject(
    request: Request,
    db: DBSession,
    current_user: TeacherUser,
    name: str = Form(...),
    description: str = Form(""),
    github_repo: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    existing = await db.execute(select(Subject.id).where(Subject.name == name.strip()))
    if existing.scalar_one_or_none() is not None:
        return templates.TemplateResponse(  # type: ignore[return-value]
            request=request,
            name="teacher_subject_form.html",
            context={"current_user": current_user, "subject": None, "error": "A subject with this name already exists."},
            status_code=422,
        )
    subject = Subject(
        name=name.strip(),
        description=description.strip() or None,
        github_repo=github_repo.strip() or None,
    )
    db.add(subject)
    await audit(db, action="create_subject", actor_id=current_user.user_id, actor_username=current_user.username, name=name)
    await db.commit()
    await db.refresh(subject)
    return RedirectResponse(url=f"/teacher/subjects/{subject.id}", status_code=303)


@router.get("/subjects/{subject_id}/edit", response_class=HTMLResponse)
async def edit_subject_page(
    request: Request,
    subject_id: int,
    db: DBSession,
    current_user: TeacherUser,
) -> HTMLResponse:
    subject = await db.get(Subject, subject_id)
    if subject is None:
        raise HTTPException(status_code=404, detail="Subject not found")
    return templates.TemplateResponse(
        request=request,
        name="teacher_subject_form.html",
        context={"current_user": current_user, "subject": subject, "error": None},
    )


@router.post("/subjects/{subject_id}/edit")
async def edit_subject(
    subject_id: int,
    db: DBSession,
    current_user: TeacherUser,
    name: str = Form(...),
    description: str = Form(""),
    github_repo: str = Form(""),
) -> RedirectResponse:
    subject = await db.get(Subject, subject_id)
    if subject is None:
        raise HTTPException(status_code=404, detail="Subject not found")
    subject.name = name.strip()
    subject.description = description.strip() or None
    subject.github_repo = github_repo.strip() or None
    await audit(db, action="edit_subject", actor_id=current_user.user_id, actor_username=current_user.username, subject_id=subject_id)
    await db.commit()
    return RedirectResponse(url=f"/teacher/subjects/{subject_id}", status_code=303)


@router.post("/subjects/{subject_id}/delete")
async def delete_subject(
    subject_id: int,
    db: DBSession,
    current_user: TeacherUser,
) -> RedirectResponse:
    subject = await db.get(Subject, subject_id)
    if subject is None:
        raise HTTPException(status_code=404, detail="Subject not found")
    await db.delete(subject)
    await audit(db, action="delete_subject", actor_id=current_user.user_id, actor_username=current_user.username, subject_id=subject_id)
    await db.commit()
    return RedirectResponse(url="/teacher", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# Assignment CRUD
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/subjects/{subject_id}/assignments/create", response_class=HTMLResponse)
async def create_assignment_page(
    request: Request,
    subject_id: int,
    db: DBSession,
    current_user: TeacherUser,
) -> HTMLResponse:
    subject = await db.get(Subject, subject_id)
    if subject is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="teacher_assignment_form.html",
        context={"current_user": current_user, "subject": subject, "assignment": None, "error": None},
    )


@router.post("/subjects/{subject_id}/assignments/create")
async def create_assignment(
    request: Request,
    subject_id: int,
    db: DBSession,
    current_user: TeacherUser,
    title: str = Form(...),
    description: str = Form(""),
    deadline: str = Form(""),
    min_grade: int = Form(0),
    max_grade: int = Form(100),
    review_mode: str = Form("none"),
    download_links_json: str = Form(""),
    max_submissions: str = Form(""),
    late_policy: str = Form("block"),
) -> RedirectResponse:
    subject = await db.get(Subject, subject_id)
    if subject is None:
        raise HTTPException(status_code=404)

    deadline_dt: datetime | None = None
    if deadline.strip():
        try:
            deadline_dt = datetime.fromisoformat(deadline.strip()).replace(tzinfo=UTC)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid deadline format")

    download_links = []
    if download_links_json.strip():
        try:
            download_links = json.loads(download_links_json.strip())
        except json.JSONDecodeError:
            pass

    config: dict = {"review_mode": review_mode, "late_policy": late_policy}
    if download_links:
        config["download_links"] = download_links
    if max_submissions.strip():
        try:
            config["max_submissions"] = int(max_submissions.strip())
        except ValueError:
            pass

    sa = SubjectsAssignment(
        subject_id=subject_id,
        title=title.strip(),
        description=description.strip() or None,
        deadline=deadline_dt,
        min_grade=min_grade,
        max_grade=max_grade,
        config=config,
    )
    db.add(sa)

    # Create StudentAssignment records for all enrolled students
    await db.flush()
    enrolled_result = await db.execute(
        select(SubjectsStudents.student_id).where(SubjectsStudents.subject_id == subject_id)
    )
    for (sid,) in enrolled_result:
        db.add(StudentAssignment(student_id=sid, subjects_assignment_id=sa.id))

    await audit(
        db, action="create_assignment", actor_id=current_user.user_id,
        actor_username=current_user.username, subject_id=subject_id, title=title,
    )
    await db.commit()
    await db.refresh(sa)
    return RedirectResponse(
        url=f"/teacher/subjects/{subject_id}/assignments/{sa.id}", status_code=303
    )


@router.get("/subjects/{subject_id}/assignments/{sa_id}/edit", response_class=HTMLResponse)
async def edit_assignment_page(
    request: Request,
    subject_id: int,
    sa_id: int,
    db: DBSession,
    current_user: TeacherUser,
) -> HTMLResponse:
    result = await db.execute(
        select(SubjectsAssignment)
        .where(SubjectsAssignment.id == sa_id, SubjectsAssignment.subject_id == subject_id)
        .options(selectinload(SubjectsAssignment.subject))
    )
    sa = result.scalar_one_or_none()
    if sa is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="teacher_assignment_form.html",
        context={"current_user": current_user, "subject": sa.subject, "assignment": sa, "error": None},
    )


@router.post("/subjects/{subject_id}/assignments/{sa_id}/edit")
async def edit_assignment(
    subject_id: int,
    sa_id: int,
    db: DBSession,
    current_user: TeacherUser,
    title: str = Form(...),
    description: str = Form(""),
    deadline: str = Form(""),
    min_grade: int = Form(0),
    max_grade: int = Form(100),
    review_mode: str = Form("none"),
    download_links_json: str = Form(""),
    max_submissions: str = Form(""),
    late_policy: str = Form("block"),
) -> RedirectResponse:
    result = await db.execute(
        select(SubjectsAssignment)
        .where(SubjectsAssignment.id == sa_id, SubjectsAssignment.subject_id == subject_id)
    )
    sa = result.scalar_one_or_none()
    if sa is None:
        raise HTTPException(status_code=404)

    deadline_dt: datetime | None = None
    if deadline.strip():
        try:
            deadline_dt = datetime.fromisoformat(deadline.strip()).replace(tzinfo=UTC)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid deadline format")

    download_links = []
    if download_links_json.strip():
        try:
            download_links = json.loads(download_links_json.strip())
        except json.JSONDecodeError:
            pass

    config = dict(sa.config)
    config["review_mode"] = review_mode
    config["late_policy"] = late_policy
    if download_links:
        config["download_links"] = download_links
    else:
        config.pop("download_links", None)
    if max_submissions.strip():
        try:
            config["max_submissions"] = int(max_submissions.strip())
        except ValueError:
            pass
    else:
        config.pop("max_submissions", None)

    sa.title = title.strip()
    sa.description = description.strip() or None
    sa.deadline = deadline_dt
    sa.min_grade = min_grade
    sa.max_grade = max_grade
    sa.config = config

    await audit(
        db, action="edit_assignment", actor_id=current_user.user_id,
        actor_username=current_user.username, sa_id=sa_id,
    )
    await db.commit()
    return RedirectResponse(url=f"/teacher/subjects/{subject_id}/assignments/{sa_id}", status_code=303)


@router.post("/subjects/{subject_id}/assignments/{sa_id}/delete")
async def delete_assignment(
    subject_id: int,
    sa_id: int,
    db: DBSession,
    current_user: TeacherUser,
) -> RedirectResponse:
    result = await db.execute(
        select(SubjectsAssignment)
        .where(SubjectsAssignment.id == sa_id, SubjectsAssignment.subject_id == subject_id)
    )
    sa = result.scalar_one_or_none()
    if sa is None:
        raise HTTPException(status_code=404)
    await db.delete(sa)
    await audit(
        db, action="delete_assignment", actor_id=current_user.user_id,
        actor_username=current_user.username, sa_id=sa_id,
    )
    await db.commit()
    return RedirectResponse(url=f"/teacher/subjects/{subject_id}", status_code=303)


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
    return templates.TemplateResponse(
        request=request,
        name="teacher_add_student.html",
        context={"current_user": current_user, "groups": groups, "error": None, "success": None},
    )


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
        return templates.TemplateResponse(  # type: ignore[return-value]
            request=request,
            name="teacher_add_student.html",
            context={
                "current_user": current_user,
                "groups": groups_result.scalars().all(),
                "error": "A student with this email already exists.",
                "success": None,
            },
            status_code=422,
        )

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
