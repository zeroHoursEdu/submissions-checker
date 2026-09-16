"""Teacher-facing portal routes."""

from __future__ import annotations

import csv
import io
import secrets
import urllib.parse
from datetime import date
from pathlib import Path
from typing import Any

import bcrypt
from fastapi import APIRouter, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import Select, and_, cast, false, func, nullsfirst, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from submissions_checker.api.authz import require_subject_access
from submissions_checker.api.dependencies import AppSettings, DBSession, TeacherUser
from submissions_checker.api.routes.teacher_disputes import count_open_disputes
from submissions_checker.core.config import get_settings
from submissions_checker.core.logging import get_logger
from submissions_checker.core.security import COOKIE_NAME, create_access_token
from submissions_checker.core.state_machine import transition
from submissions_checker.core.templates import render
from submissions_checker.db.models import (
    EntityType,
    FeedbackRequest,
    FeedbackResponse,
    FeedbackToken,
    OutboxMessage,
    QuizAttempt,
    QuizAttemptSnapshot,
    Semester,
    Student,
    StudentAssignment,
    Subject,
    SubjectGradebookStats,
    SubjectsAssignment,
    SubjectsStudents,
    SubjectTestStudent,
    Submission,
    User,
    UserLogin,
)
from submissions_checker.db.models.enums import (
    OutboxEventType,
    OutboxMessageState,
    SubjectStatus,
    SubmissionStatus,
    UserRole,
)
from submissions_checker.db.models.group import Group
from submissions_checker.services.audit import audit
from submissions_checker.services.config_apply import ConfigApplyService
from submissions_checker.services.gradebook import (
    build_student_grid,
    fetch_grid_rows,
)
from submissions_checker.services.grading import finalize_grade
from submissions_checker.services.storage import StorageService

UPLOADS_DIR = Path("uploads")

logger = get_logger(__name__)
router = APIRouter(prefix="/teacher", tags=["teacher-portal"])

_SAMPLE_CSV = "student_group,student_name,student_surname,email\nIT-21,Ivan,Petrenko,ivan@example.com\nIT-21,Olena,Kovalenko,olena@example.com\n"


def _generate_password() -> str:
    return secrets.token_urlsafe(9)


def _query_int(request: Request, name: str) -> int:
    """Read a non-negative integer flash value from the query string."""
    raw = request.query_params.get(name, "0")
    return int(raw) if raw.isdigit() else 0


async def _generate_username(base: str, db: DBSession) -> str:
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
            func.count(Student.id).label("enrolled_count"),
        )
        .outerjoin(SubjectsStudents, SubjectsStudents.subject_id == Subject.id)
        .outerjoin(
            Student,
            and_(Student.id == SubjectsStudents.student_id, Student.type == EntityType.REAL),
        )
        .where(Subject.status == SubjectStatus.ACTIVE)
        .group_by(Subject.id, Subject.name, Subject.description, Subject.owner_id)
        .order_by(Subject.name)
    )
    subjects = [row._asdict() for row in result]

    apply_result = request.query_params.get("apply_result")
    apply_error = request.query_params.get("apply_error")
    if apply_error:
        apply_error = urllib.parse.unquote(apply_error)

    return render(
        request,
        "teacher_dashboard.html",
        {
            "current_user": current_user,
            "subjects": subjects,
            "apply_result": apply_result,
            "apply_error": apply_error,
            "open_dispute_count": await count_open_disputes(db, current_user),
        },
    )


@router.post("/subjects/apply-config")
async def apply_subject_config(
    request: Request,
    db: DBSession,
    current_user: TeacherUser,
    settings: AppSettings,
    config_zip: UploadFile,
) -> RedirectResponse:
    storage = StorageService(settings) if settings.s3_endpoint_url else None
    service = ConfigApplyService(storage, plugins_dir=Path(settings.plugins_dir))
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
    subject = await require_subject_access(db, subject_id, current_user)
    subject.status = SubjectStatus.DELETED
    await db.commit()
    return RedirectResponse("/teacher", status_code=303)


@router.post("/subjects/{subject_id}/test-student")
async def provision_test_student(
    subject_id: int,
    request: Request,
    db: DBSession,
    current_user: TeacherUser,
) -> RedirectResponse:
    await require_subject_access(db, subject_id, current_user)

    existing = await db.execute(
        select(SubjectTestStudent).where(SubjectTestStudent.subject_id == subject_id)
    )
    if existing.scalar_one_or_none() is not None:
        return RedirectResponse(
            f"/teacher/subjects/{subject_id}?test_student=existing", status_code=303
        )

    test_group_result = await db.execute(select(Group).where(Group.name == "__TEST__"))
    test_group = test_group_result.scalar_one()

    password = _generate_password()
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode()
    username = f"test_{subject_id}"

    student = Student(
        group_id=test_group.id,
        full_name=f"Test Student (subject {subject_id})",
        email=f"test+{subject_id}@test.internal",
        type=EntityType.TEST,
    )
    db.add(student)
    await db.flush()

    user = User(
        username=username,
        password_hash=password_hash,
        role=UserRole.STUDENT,
        student_id=student.id,
    )
    db.add(user)
    await db.flush()

    db.add(SubjectsStudents(subject_id=subject_id, student_id=student.id))
    await db.flush()

    form = await request.form()
    sa_rows_result = await db.execute(
        select(SubjectsAssignment.id, SubjectsAssignment.code, SubjectsAssignment.config).where(
            SubjectsAssignment.subject_id == subject_id
        )
    )
    for sa_id_val, sa_code, sa_config in sa_rows_result:
        sa_config = sa_config or {}
        variants: dict[str, Any] = sa_config.get("variants") or {}
        variant: str | None = None
        if variants:
            submitted = form.get(f"variant_{sa_code}")
            if isinstance(submitted, str) and submitted in variants:
                variant = submitted
            elif sa_config.get("variants_required"):
                # Always assign a valid variant for required assignments, even if the
                # teacher didn't touch the selector — otherwise the test student can't
                # submit at all (docs/known_bugs.md #12b).
                variant = sorted(variants)[0]
        db.add(
            StudentAssignment(
                student_id=student.id, subjects_assignment_id=sa_id_val, variant=variant
            )
        )

    db.add(
        SubjectTestStudent(subject_id=subject_id, student_id=student.id, plain_password=password)
    )
    await db.commit()

    return RedirectResponse(f"/teacher/subjects/{subject_id}?test_student=created", status_code=303)


@router.post("/subjects/{subject_id}/test-student/enter")
async def enter_as_test_student(
    subject_id: int,
    db: DBSession,
    current_user: TeacherUser,
) -> RedirectResponse:
    await require_subject_access(db, subject_id, current_user)

    sts_result = await db.execute(
        select(SubjectTestStudent).where(SubjectTestStudent.subject_id == subject_id)
    )
    sts = sts_result.scalar_one_or_none()
    if sts is None:
        raise HTTPException(status_code=404, detail="No test student provisioned for this subject")

    user_result = await db.execute(select(User).where(User.student_id == sts.student_id))
    test_user = user_result.scalar_one()

    token = create_access_token(test_user.id, test_user.username, test_user.role.value)
    response = RedirectResponse("/portal", status_code=303)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="strict",
        secure=get_settings().cookie_secure,
        max_age=8 * 3600,
    )
    return response


@router.get("/subjects/{subject_id}", response_class=HTMLResponse)
async def teacher_subject(
    request: Request, subject_id: int, db: DBSession, current_user: TeacherUser
) -> HTMLResponse:
    subject = await require_subject_access(db, subject_id, current_user)

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

    test_student_info = None
    if subject.owner_id == current_user.user_id:
        sts_result = await db.execute(
            select(SubjectTestStudent, User.username)
            .join(User, User.student_id == SubjectTestStudent.student_id)
            .where(SubjectTestStudent.subject_id == subject_id)
        )
        sts_row = sts_result.one_or_none()
        if sts_row is not None:
            test_student_info = {
                "username": sts_row.username,
                "plain_password": sts_row.SubjectTestStudent.plain_password,
            }

    test_student_flash = request.query_params.get("test_student")

    # Enrolment result, round-tripped through the redirect from the CSV import.
    enroll_result = None
    if request.query_params.get("enrolled") is not None:
        raw_rows = request.query_params.get("rejected_rows", "")
        rejected_rows = []
        for pair in raw_rows.split(";") if raw_rows else []:
            line, _, reason = pair.partition(":")
            if line.isdigit():
                rejected_rows.append({"line": int(line), "reason": reason})
        rejected_total = _query_int(request, "rejected")
        enroll_result = {
            "enrolled": _query_int(request, "enrolled"),
            "already": _query_int(request, "already"),
            "rejected": rejected_total,
            "rejected_rows": rejected_rows,
            "rejected_overflow": max(rejected_total - len(rejected_rows), 0),
        }

    cached_stats = await db.get(SubjectGradebookStats, subject_id)
    grid_rows = await fetch_grid_rows(db, subject_id)
    student_grid = build_student_grid(grid_rows)

    task_pending_counts: dict[int, int] = {}
    for row in grid_rows:
        if (
            row.grade is None
            and row.submission_status is not None
            and row.submission_status not in (SubmissionStatus.COMPLETED, SubmissionStatus.FAILED)
        ):
            task_pending_counts[row.assignment_id] = (
                task_pending_counts.get(row.assignment_id, 0) + 1
            )

    default_tab = (
        "operations"
        if (enroll_result or test_student_flash or feedback_sent or feedback_error)
        else "panel"
    )

    return render(
        request,
        "teacher_subject.html",
        {
            "current_user": current_user,
            "subject": subject,
            "assignments": assignments,
            "current_semester": current_semester,
            "feedback_request": feedback_request,
            "feedback_sent": feedback_sent,
            "feedback_error": feedback_error,
            "test_student_info": test_student_info,
            "test_student_flash": test_student_flash,
            "enroll_result": enroll_result,
            "cached_stats": cached_stats,
            "student_grid": student_grid,
            "task_pending_counts": task_pending_counts,
            "default_tab": default_tab,
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
    await require_subject_access(db, subject_id, current_user)

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
        .where(SubjectsStudents.subject_id == subject_id, Student.type == EntityType.REAL)
        .order_by(nullsfirst(StudentAssignment.grade.asc()), Student.full_name)
    )
    rows = [row._asdict() for row in rows_result]

    # Load violation flags: for each student_assignment, find if any attempt has violations
    sa_id_list = [r["student_assignment_id"] for r in rows if r["student_assignment_id"]]
    violation_flags: dict[int, dict[str, Any]] = {}
    if sa_id_list:
        viol_result = await db.execute(
            select(
                Submission.students_assignment_id,
                QuizAttempt.violations,
            )
            .join(Submission, Submission.id == QuizAttempt.submission_id)
            .where(
                Submission.students_assignment_id.in_(sa_id_list),
                # violations is a NOT NULL JSONB object (default {}); select rows
                # whose object is non-empty. PostgreSQL has no jsonb_object_length,
                # so compare against the empty object directly.
                QuizAttempt.violations != cast({}, JSONB),
            )
            .order_by(QuizAttempt.started_at.desc())
        )
        for vr in viol_result:
            sa_id_val = vr.students_assignment_id
            if sa_id_val not in violation_flags:
                violation_flags[sa_id_val] = vr.violations or {}

    # Load proctoring snapshot thumbnails grouped by student_assignment.
    snapshot_flags: dict[int, list[dict[str, Any]]] = {}
    if sa_id_list:
        snap_result = await db.execute(
            select(
                Submission.students_assignment_id,
                QuizAttemptSnapshot.id,
                QuizAttemptSnapshot.event_type,
                QuizAttemptSnapshot.captured_at,
            )
            .join(QuizAttempt, QuizAttempt.id == QuizAttemptSnapshot.attempt_id)
            .join(Submission, Submission.id == QuizAttempt.submission_id)
            .where(Submission.students_assignment_id.in_(sa_id_list))
            .order_by(QuizAttemptSnapshot.captured_at.desc())
        )
        for sr in snap_result:
            # Address the application's authenticated endpoint, never object storage —
            # an evidence link must not outlive the viewer's authorization.
            snapshot_flags.setdefault(sr.students_assignment_id, []).append(
                {"event_type": sr.event_type, "url": f"/teacher/proctoring/snapshots/{sr.id}"}
            )

    return render(
        request,
        "teacher_assignment.html",
        {
            "current_user": current_user,
            "assignment": assignment,
            "subject_id": subject_id,
            "rows": rows,
            "violation_flags": violation_flags,
            "snapshot_flags": snapshot_flags,
        },
    )


_SNAPSHOT_CONTENT_TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


@router.get("/proctoring/snapshots/{snapshot_id}")
async def proctoring_snapshot(
    snapshot_id: int,
    db: DBSession,
    current_user: TeacherUser,
    settings: AppSettings,
) -> Response:
    """Stream one webcam evidence frame to a teacher authorized for its subject.

    Evidence is private: object storage is not internet-reachable and objects carry no
    public ACL, so this endpoint is the only way to read a frame. Authorization is
    checked against the owning subject before any bytes are fetched.
    """
    row = (
        await db.execute(
            select(QuizAttemptSnapshot, SubjectsAssignment.subject_id)
            .join(QuizAttempt, QuizAttempt.id == QuizAttemptSnapshot.attempt_id)
            .join(Submission, Submission.id == QuizAttempt.submission_id)
            .join(StudentAssignment, StudentAssignment.id == Submission.students_assignment_id)
            .join(
                SubjectsAssignment,
                SubjectsAssignment.id == StudentAssignment.subjects_assignment_id,
            )
            .where(QuizAttemptSnapshot.id == snapshot_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    snapshot, subject_id = row
    await require_subject_access(db, subject_id, current_user)

    if not settings.s3_endpoint_url:
        raise HTTPException(status_code=404, detail="Snapshot storage is not configured")

    storage = StorageService(settings)
    try:
        data = await storage.download_bytes(snapshot.s3_key)
    except Exception as exc:  # object missing or storage unreachable
        logger.warning("proctoring_snapshot_unreadable", snapshot_id=snapshot_id, error=str(exc))
        raise HTTPException(status_code=404, detail="Snapshot is no longer available") from exc

    extension = snapshot.s3_key.rsplit(".", 1)[-1].lower()
    return Response(
        content=data,
        media_type=_SNAPSHOT_CONTENT_TYPES.get(extension, "application/octet-stream"),
        # Evidence is per-viewer authorized; never let a shared cache hold it.
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/students/sample.csv")
async def download_sample_csv(current_user: TeacherUser) -> StreamingResponse:
    """Return a sample CSV template for teachers to share with their students."""
    return StreamingResponse(
        iter([_SAMPLE_CSV]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=students.csv"},
    )


# Enrolment rejections are round-tripped through the redirect URL, like apply_error.
# The cap keeps a CSV full of bad addresses from building a URL long enough to be
# truncated by a browser or proxy.
_MAX_REPORTED_REJECTIONS = 20

# RFC 6761 reserves example.invalid: it can never resolve or belong to a real
# person, so an unedited template enrols nobody and says so.
_TEMPLATE_PLACEHOLDER_DOMAIN = "example.invalid"


def _sorted_variant_ids(variant_ids: set[str]) -> list[str]:
    """Order variant ids numerically when they all look like numbers, else lexically.

    Config keys are strings, so plain sorting would put "10" before "2".
    """
    if variant_ids and all(v.lstrip("-").isdigit() for v in variant_ids):
        return sorted(variant_ids, key=int)
    return sorted(variant_ids)


async def _ensure_assignment_rows(
    db: DBSession,
    student_id: int,
    subject_assignment_ids: list[int],
    variant: str | None,
) -> None:
    """Give a student one students_assignments row per assignment of the subject.

    Mirrors the fan-out `enroll_student` performs, and additionally writes
    ``variant`` to every row when one was supplied. An empty variant leaves any
    stored value alone, so re-enrolling never flattens per-assignment variants
    that were set elsewhere.
    """
    for sa_id in subject_assignment_ids:
        existing = await db.execute(
            select(StudentAssignment).where(
                StudentAssignment.student_id == student_id,
                StudentAssignment.subjects_assignment_id == sa_id,
            )
        )
        student_assignment = existing.scalar_one_or_none()
        if student_assignment is None:
            student_assignment = StudentAssignment(
                student_id=student_id, subjects_assignment_id=sa_id
            )
            db.add(student_assignment)
        if variant:
            student_assignment.variant = variant


@router.get("/subjects/{subject_id}/students/template.csv")
async def download_subject_enrollment_template(
    subject_id: int,
    db: DBSession,
    current_user: TeacherUser,
) -> StreamingResponse:
    """Example enrolment CSV, with the subject's real variant ids.

    Columns: email, variant. One row per variant declared anywhere in the
    subject's config, so the ids a teacher copies are the ones the checker
    accepts. A subject with no variants still gets rows, with the cell empty,
    so the expected shape is obvious.
    """
    await require_subject_access(db, subject_id, current_user)

    # `variants` is copied into each assignment's config at apply time
    # (config_apply._build_assignment_config), so the assignment rows carry the
    # same ids as the plugin config and no second lookup is needed.
    assignments_result = await db.execute(
        select(SubjectsAssignment.config).where(SubjectsAssignment.subject_id == subject_id)
    )
    variant_ids: set[str] = set()
    for (config,) in assignments_result:
        for key in (config or {}).get("variants") or {}:
            variant_ids.add(str(key))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["email", "variant"])
    if variant_ids:
        for index, variant in enumerate(_sorted_variant_ids(variant_ids), start=1):
            writer.writerow([f"student{index}@{_TEMPLATE_PLACEHOLDER_DOMAIN}", variant])
    else:
        for index in (1, 2):
            writer.writerow([f"student{index}@{_TEMPLATE_PLACEHOLDER_DOMAIN}", ""])

    return StreamingResponse(
        iter([output.getvalue()]),
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
    """Enrol existing students into a subject from an ``email,variant`` CSV.

    Enrol-only: the e-mail must already belong to a registered student. This
    endpoint never creates students, groups or accounts and never sends an
    invitation — that stays with POST /teacher/students/import — so it can be
    re-run freely without re-inviting anyone.
    """
    await require_subject_access(db, subject_id, current_user)

    if file.size and file.size > 1_048_576:
        raise HTTPException(status_code=413, detail="File too large (max 1 MB)")

    content = await file.read()
    try:
        text_content = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="File must be UTF-8 encoded") from exc

    reader = csv.DictReader(io.StringIO(text_content))
    # Accept any capitalisation or stray spacing in the header: map the normalised
    # column name back to the key DictReader actually produced.
    columns = {(name or "").strip().lower(): name for name in (reader.fieldnames or [])}
    if "email" not in columns:
        raise HTTPException(status_code=422, detail="Missing CSV columns: email")
    email_key = columns["email"]
    variant_key = columns.get("variant")

    sa_ids_result = await db.execute(
        select(SubjectsAssignment.id).where(SubjectsAssignment.subject_id == subject_id)
    )
    subject_assignment_ids = [row[0] for row in sa_ids_result]

    enrolled_count = 0
    already_count = 0
    rejections: list[tuple[int, str]] = []

    # The header is line 1, so data rows start at 2 — the number the teacher
    # sees in their spreadsheet.
    for line_number, row in enumerate(reader, start=2):
        email = (row.get(email_key) or "").strip().lower()
        raw_variant = (row.get(variant_key) or "").strip() if variant_key else ""
        variant = raw_variant or None

        if not email:
            rejections.append((line_number, "empty"))
            continue

        student_result = await db.execute(select(Student).where(Student.email == email))
        student = student_result.scalar_one_or_none()
        if student is None:
            rejections.append((line_number, "unknown"))
            continue

        enrollment_result = await db.execute(
            select(SubjectsStudents).where(
                SubjectsStudents.subject_id == subject_id,
                SubjectsStudents.student_id == student.id,
            )
        )
        if enrollment_result.scalar_one_or_none() is None:
            db.add(SubjectsStudents(subject_id=subject_id, student_id=student.id))
            enrolled_count += 1
        else:
            already_count += 1

        await _ensure_assignment_rows(db, student.id, subject_assignment_ids, variant)
        await db.flush()

    await db.commit()

    reported = rejections[:_MAX_REPORTED_REJECTIONS]
    params = {
        "enrolled": str(enrolled_count),
        "already": str(already_count),
        "rejected": str(len(rejections)),
    }
    if reported:
        params["rejected_rows"] = ";".join(f"{line}:{reason}" for line, reason in reported)
    return RedirectResponse(
        url=f"/teacher/subjects/{subject_id}?{urllib.parse.urlencode(params)}",
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
    """Student registration overview: list students with account/email/login status,
    scoped to students enrolled in subjects the current teacher owns (ADMIN sees all)."""
    query = (
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
    )
    if current_user.role != UserRole.ADMIN:
        query = query.join(SubjectsStudents, SubjectsStudents.student_id == Student.id).join(
            Subject,
            and_(
                Subject.id == SubjectsStudents.subject_id,
                Subject.owner_id == current_user.user_id,
            ),
        )
    query = query.group_by(
        Student.id, Group.name, User.username, User.is_active, OutboxMessage.state
    ).order_by(Student.created_at.desc())
    rows_result = await db.execute(query)
    students = [row._asdict() for row in rows_result]

    return render(
        request,
        "teacher_students.html",
        {
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
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="File must be UTF-8 encoded") from exc

    reader = csv.DictReader(io.StringIO(text_content))
    required = {"student_group", "student_name", "student_surname", "email"}
    if not required.issubset(set(reader.fieldnames or [])):
        missing = required - set(reader.fieldnames or [])
        raise HTTPException(
            status_code=422, detail=f"Missing CSV columns: {', '.join(sorted(missing))}"
        )

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
            selectinload(Submission.students_assignment)
            .selectinload(StudentAssignment.subjects_assignment)
            .selectinload(SubjectsAssignment.subject),
            selectinload(Submission.students_assignment).selectinload(StudentAssignment.student),
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

    subject = subjects_assignment.subject
    if current_user.role != UserRole.ADMIN and subject.owner_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="Not authorized for this subject")

    return render(
        request,
        "teacher_submission_review.html",
        {
            "current_user": current_user,
            "submission": submission,
            "student": sa.student,
            "assignment": subjects_assignment,
            "subject": subjects_assignment.subject,
        },
    )


@router.get("/submissions/{submission_id}/download")
async def teacher_download_submission(
    submission_id: int,
    db: DBSession,
    current_user: TeacherUser,
) -> FileResponse:
    """Serve the student's uploaded archive to the reviewing teacher.

    Under the quiz-first review modes nothing runs the submission, so reading the attached
    report and sources IS the teacher's review — without this the review page can only name
    the file.
    """
    result = await db.execute(
        select(Submission)
        .where(Submission.id == submission_id)
        .options(
            selectinload(Submission.students_assignment)
            .selectinload(StudentAssignment.subjects_assignment)
            .selectinload(SubjectsAssignment.subject)
        )
    )
    submission = result.scalar_one_or_none()
    if submission is None:
        raise HTTPException(status_code=404)

    subject = submission.students_assignment.subjects_assignment.subject
    if current_user.role != UserRole.ADMIN and subject.owner_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="Not authorized for this subject")

    saved_as = (submission.source_metadata or {}).get("saved_as")
    if not saved_as:
        raise HTTPException(status_code=404, detail="Submission has no stored file")

    uploads_root = UPLOADS_DIR.resolve()
    path = (uploads_root / saved_as).resolve()
    # saved_as is server-generated, but never trust a stored path to stay inside its root.
    if not path.is_file() or uploads_root not in path.parents:
        raise HTTPException(status_code=404, detail="Submission file is no longer available")

    original = (submission.source_metadata or {}).get("original_filename") or path.name
    return FileResponse(
        path,
        media_type="application/zip",
        filename=Path(original).name,
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

    await require_subject_access(db, subjects_assignment.subject_id, current_user)

    clean_reason = reason.strip()
    if action == "approve":
        has_quiz = False
        if submission.plugin_config and subjects_assignment.code:
            asgn_cfg = submission.plugin_config.config.get("assignments", {}).get(
                subjects_assignment.code, {}
            )
            has_quiz = bool(asgn_cfg.get("quiz", {}).get("questions"))
        if has_quiz:
            # Under `quiz_then_teacher` the quiz already happened and this review IS the last
            # step — without this guard such a submission would be sent back into its quiz on
            # every approval and could never complete.
            already_passed = await db.scalar(
                select(QuizAttempt.id)
                .where(
                    QuizAttempt.submission_id == submission.id,
                    QuizAttempt.is_passed.is_(True),
                )
                .limit(1)
            )
            if already_passed is not None:
                has_quiz = False
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

    # A teacher approval that completes the submission (no quiz) finalizes the grade now;
    # approvals that route to a quiz finalize on quiz completion instead.
    if submission.status == SubmissionStatus.COMPLETED:
        await finalize_grade(db, submission)

    # Queue email notification to student
    db.add(
        OutboxMessage(
            event_type=OutboxEventType.SUBMISSION_REVIEWED,
            state=OutboxMessageState.PENDING,
            payload={
                "submission_id": submission_id,
                "action": action,
                "reason": clean_reason,
            },
        )
    )

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
    await require_subject_access(db, subject_id, current_user)
    existing = await db.execute(
        select(SubjectsStudents).where(
            SubjectsStudents.subject_id == subject_id,
            SubjectsStudents.student_id == student_id_param,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(SubjectsStudents(subject_id=subject_id, student_id=student_id_param))
        assignments_result = await db.execute(
            select(SubjectsAssignment.id).where(SubjectsAssignment.subject_id == subject_id)
        )
        await _ensure_assignment_rows(
            db, student_id_param, [row[0] for row in assignments_result], variant=None
        )
        await audit(
            db,
            action="enroll_student",
            actor_id=current_user.user_id,
            actor_username=current_user.username,
            subject_id=subject_id,
            student_id=student_id_param,
        )
        await db.commit()
    return RedirectResponse(url=f"/teacher/subjects/{subject_id}", status_code=303)


@router.get("/subjects/{subject_id}/students/search")
async def search_students_by_email(
    subject_id: int,
    q: str,
    db: DBSession,
    current_user: TeacherUser,
) -> list[dict[str, Any]]:
    """Autocomplete source for the Операції tab's search-enroll flow.

    3+ chars, ILIKE on email — enroll-only, never creates students (mirrors
    the CSV import route's enroll-only contract).
    """
    await require_subject_access(db, subject_id, current_user)
    if len(q) < 3:
        raise HTTPException(status_code=422, detail="Query must be at least 3 characters")

    result = await db.execute(
        select(Student.id, Student.full_name, Student.email)
        .where(Student.type == EntityType.REAL, Student.email.ilike(f"%{q}%"))
        .order_by(Student.full_name)
        .limit(10)
    )
    return [{"id": row.id, "full_name": row.full_name, "email": row.email} for row in result]


@router.post("/subjects/{subject_id}/students/enroll-by-search")
async def enroll_student_by_search(
    subject_id: int,
    db: DBSession,
    current_user: TeacherUser,
    student_id: int = Form(...),
    variant: str = Form(""),
) -> RedirectResponse:
    """Enroll one student found via search, reusing the same enrollment logic
    button-enroll and CSV-enroll already share (_ensure_assignment_rows)."""
    await require_subject_access(db, subject_id, current_user)

    sa_rows_result = await db.execute(
        select(SubjectsAssignment.id, SubjectsAssignment.config).where(
            SubjectsAssignment.subject_id == subject_id
        )
    )
    sa_rows = sa_rows_result.all()
    needs_variant = any((row.config or {}).get("variants_required") for row in sa_rows)
    clean_variant = variant.strip() or None
    if needs_variant and not clean_variant:
        raise HTTPException(status_code=422, detail="This subject requires a variant")

    existing = await db.execute(
        select(SubjectsStudents).where(
            SubjectsStudents.subject_id == subject_id,
            SubjectsStudents.student_id == student_id,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(SubjectsStudents(subject_id=subject_id, student_id=student_id))
        await _ensure_assignment_rows(db, student_id, [row.id for row in sa_rows], clean_variant)
        await audit(
            db,
            action="enroll_student_by_search",
            actor_id=current_user.user_id,
            actor_username=current_user.username,
            subject_id=subject_id,
            student_id=student_id,
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
    await require_subject_access(db, subject_id, current_user)
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
            db,
            action="unenroll_student",
            actor_id=current_user.user_id,
            actor_username=current_user.username,
            subject_id=subject_id,
            student_id=student_id_param,
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
    subject = await require_subject_access(db, subject_id, current_user)

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
                Submission.created_at
                == select(func.max(Submission.created_at))
                .where(Submission.students_assignment_id == StudentAssignment.id)
                .correlate(StudentAssignment)
                .scalar_subquery(),
            ),
        )
        .where(SubjectsStudents.subject_id == subject_id)
        .order_by(Group.name, Student.full_name, SubjectsAssignment.title)
    )
    rows = result.all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["Student", "Email", "Group", "Assignment", "Grade", "Max Grade", "Status", "Submitted At"]
    )
    for r in rows:
        writer.writerow(
            [
                r.full_name,
                r.email,
                r.group_name,
                r.assignment_title,
                r.grade if r.grade is not None else "",
                r.max_grade,
                r.submission_status or "",
                r.submitted_at.strftime("%Y-%m-%d %H:%M") if r.submitted_at else "",
            ]
        )
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
    return render(
        request,
        "teacher_add_student.html",
        {"current_user": current_user, "groups": groups, "error": None, "success": None},
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
) -> HTMLResponse | RedirectResponse:
    email = email.strip().lower()
    group_name = group_name.strip()

    existing = await db.execute(select(Student.id).where(Student.email == email))
    if existing.scalar_one_or_none() is not None:
        groups_result = await db.execute(select(Group).order_by(Group.name))
        return render(
            request,
            "teacher_add_student.html",
            {
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

    db.add(
        OutboxMessage(
            event_type=OutboxEventType.SEND_CREDENTIALS,
            state=OutboxMessageState.PENDING,
            payload={
                "student_email": email,
                "full_name": full_name,
                "username": username,
                "password": password,
            },
        )
    )

    await audit(
        db,
        action="add_student",
        actor_id=current_user.user_id,
        actor_username=current_user.username,
        student_email=email,
    )
    await db.commit()

    return RedirectResponse(url="/teacher/students?imported=1&skipped=0", status_code=303)


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


def _current_semester_query() -> Select[tuple[Semester]]:
    today = date.today()
    return select(Semester).where(Semester.start_date <= today, Semester.end_date >= today)


@router.post("/subjects/{subject_id}/feedback/request")
async def request_feedback(
    subject_id: int,
    request: Request,
    db: DBSession,
    current_user: TeacherUser,
) -> RedirectResponse:
    await require_subject_access(db, subject_id, current_user)

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
        db.add(
            FeedbackToken(
                feedback_request_id=feedback_request.id,
                student_id=student.id,
                token=token_str,
            )
        )
        await db.flush()
        token_result = await db.execute(
            select(FeedbackToken).where(FeedbackToken.token == token_str)
        )
        saved_token = token_result.scalar_one()
        db.add(
            OutboxMessage(
                event_type=OutboxEventType.FEEDBACK_REQUEST_SENT,
                state=OutboxMessageState.PENDING,
                payload={"feedback_token_id": saved_token.id},
            )
        )

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
    subject = await require_subject_access(db, subject_id, current_user)

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

    return render(
        request,
        "teacher_feedback_view.html",
        {
            "current_user": current_user,
            "subject": subject,
            "feedback_request": feedback_request,
            "responses": responses,
            "avg_rating": avg_rating,
            "current_semester": current_semester,
        },
    )


@router.get("/subjects/{subject_id}/feedback/export.csv")
async def export_feedback_csv(
    subject_id: int,
    db: DBSession,
    current_user: TeacherUser,
) -> StreamingResponse:
    await require_subject_access(db, subject_id, current_user)

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
    writer.writerow(
        [
            "student_name",
            "student_email",
            "rating",
            "went_well",
            "went_bad",
            "to_change",
            "submitted_at",
        ]
    )
    for resp, student in rows:
        writer.writerow(
            [
                student.full_name,
                student.email,
                resp.rating,
                resp.went_well,
                resp.went_bad,
                resp.to_change,
                resp.submitted_at.isoformat(),
            ]
        )

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=feedback_{subject_id}.csv"},
    )
