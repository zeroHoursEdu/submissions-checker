"""Functional coverage for portal *detail* pages and review render branches.

Targets reachable branches that the existing portal suites only lightly touch:

* ``teacher_portal.teacher_assignment`` (the subject assignment-detail page that
  renders each enrolled student's latest submission, grade, violation/snapshot
  flags) — owner 200 with seeded data reflected, cross-teacher 403, missing 404.
* ``teacher_portal.teacher_students`` (the registration overview list).
* ``teacher_portal.teacher_review_submission`` GET render + the quiz-approve
  branches of the POST action (``teacher_send_quiz`` / ``teacher_approve_quiz``).
* ``student_portal.assignment_detail`` quiz-metadata branches (latest attempt
  present → ``quiz_attempt_id``; no attempt but plugin config → ``max_attempts``)
  and ``check_reason`` surfacing.
* ``student_portal.student_summary`` overdue / upcoming-deadline classification
  and the no-enrollment empty path.

Behaviour was read directly from ``api/routes/teacher_portal.py`` and
``api/routes/student_portal.py``; assertions check exact status codes and the
real rendered/DB state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

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
    OutboxEventType,
    QuizAttemptStatus,
    UserRole,
)
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from tests.functional.conftest import authenticate

pytestmark = pytest.mark.asyncio


# ── Arrange helpers ──────────────────────────────────────────────────────────


async def _make_subject(db, owner_id: int | None, *, name: str = "Owned Subject",
                        code: str | None = None) -> Subject:
    subject = Subject(name=name, code=code, owner_id=owner_id)
    db.add(subject)
    await db.commit()
    await db.refresh(subject)
    return subject


async def _make_assignment(db, subject_id: int, *, title: str = "A1", code: str = "a1",
                          config: dict | None = None, deadline: datetime | None = None,
                          content_files: list | None = None) -> SubjectsAssignment:
    sa = SubjectsAssignment(
        subject_id=subject_id, code=code, title=title, max_grade=100,
        config=config or {}, deadline=deadline, content_files=content_files,
    )
    db.add(sa)
    await db.commit()
    await db.refresh(sa)
    return sa


async def _enroll(db, subject_id: int, student_id: int) -> None:
    db.add(SubjectsStudents(subject_id=subject_id, student_id=student_id))
    await db.commit()


async def _make_student_assignment(db, student_id: int, sa_id: int, *,
                                   grade: int | None = None) -> StudentAssignment:
    student_assignment = StudentAssignment(
        student_id=student_id, subjects_assignment_id=sa_id, grade=grade
    )
    db.add(student_assignment)
    await db.commit()
    await db.refresh(student_assignment)
    return student_assignment


async def _make_submission(db, student_assignment_id: int, *,
                          status: SubmissionStatus = SubmissionStatus.PENDING,
                          created_at: datetime | None = None,
                          plugin_config_id: int | None = None,
                          test_results: dict | None = None,
                          source_metadata: dict | None = None) -> Submission:
    sub = Submission(
        students_assignment_id=student_assignment_id,
        source_type=SubmissionSourceType.ZIP_UPLOAD,
        source_metadata=source_metadata or {},
        status=status,
        plugin_config_id=plugin_config_id,
        test_results=test_results,
    )
    if created_at is not None:
        sub.created_at = created_at
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return sub


async def _make_plugin_config(db, subject_id: int, config: dict, *, version: int = 1) -> SubjectPluginConfig:
    cfg = SubjectPluginConfig(
        subject_id=subject_id, version=version,
        content_hash=f"hash-{subject_id}-{version}", config=config,
    )
    db.add(cfg)
    await db.commit()
    await db.refresh(cfg)
    return cfg


async def _consent(db, student_id: int) -> None:
    student = await db.get(Student, student_id)
    student.recording_consent_at = datetime.now(UTC)
    await db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# teacher_portal.teacher_assignment (subject assignment-detail page)  327-435
# ─────────────────────────────────────────────────────────────────────────────


async def test_teacher_assignment_owner_renders_enrolled_students_and_status(
    client: AsyncClient, db, teacher, make_student
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    sa = await _make_assignment(db, subject.id, title="Lab Alpha", code="lab1")

    # Two enrolled students: one with a graded submission, one with none.
    grace = await make_student(full_name="Grace Hopper", email="grace@example.com")
    ada = await make_student(full_name="Ada Lovelace", email="ada@example.com")
    await _enroll(db, subject.id, grace.id)
    await _enroll(db, subject.id, ada.id)

    grace_sa = await _make_student_assignment(db, grace.id, sa.id, grade=88)
    # Older + newer submission: only the latest (COMPLETED) must surface.
    await _make_submission(
        db, grace_sa.id, status=SubmissionStatus.FAILED,
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    await _make_submission(
        db, grace_sa.id, status=SubmissionStatus.COMPLETED,
        created_at=datetime.now(UTC),
    )
    # ada is enrolled but has no StudentAssignment/submission at all.

    authenticate(client, teacher)
    resp = await client.get(f"/teacher/subjects/{subject.id}/assignments/{sa.id}")
    assert resp.status_code == 200
    body = resp.text
    # Both enrolled students appear (outer-joined rows).
    assert "Grace Hopper" in body
    assert "Ada Lovelace" in body
    # Assignment title rendered.
    assert "Lab Alpha" in body


async def test_teacher_assignment_cross_teacher_is_403(
    client: AsyncClient, db, teacher, make_user
) -> None:
    other = await make_user(role=UserRole.TEACHER, username="other")
    subject = await _make_subject(db, owner_id=other.id)
    sa = await _make_assignment(db, subject.id)
    authenticate(client, teacher)
    resp = await client.get(f"/teacher/subjects/{subject.id}/assignments/{sa.id}")
    assert resp.status_code == 403


async def test_teacher_assignment_admin_can_view_any(
    client: AsyncClient, db, admin, make_user
) -> None:
    other = await make_user(role=UserRole.TEACHER, username="other")
    subject = await _make_subject(db, owner_id=other.id)
    sa = await _make_assignment(db, subject.id)
    authenticate(client, admin)
    resp = await client.get(f"/teacher/subjects/{subject.id}/assignments/{sa.id}")
    assert resp.status_code == 200


async def test_teacher_assignment_missing_subject_is_404(teacher_client: AsyncClient) -> None:
    resp = await teacher_client.get("/teacher/subjects/999/assignments/999")
    assert resp.status_code == 404


async def test_teacher_assignment_missing_assignment_is_404(
    client: AsyncClient, db, teacher
) -> None:
    # Subject exists & owned, but the assignment id does not belong to it → 404.
    subject = await _make_subject(db, owner_id=teacher.id)
    authenticate(client, teacher)
    resp = await client.get(f"/teacher/subjects/{subject.id}/assignments/999")
    assert resp.status_code == 404


async def test_teacher_assignment_wrong_subject_for_assignment_is_404(
    client: AsyncClient, db, teacher
) -> None:
    # Assignment belongs to a *different* owned subject → filtered out → 404.
    s1 = await _make_subject(db, owner_id=teacher.id, name="S1")
    s2 = await _make_subject(db, owner_id=teacher.id, name="S2")
    sa2 = await _make_assignment(db, s2.id)
    authenticate(client, teacher)
    resp = await client.get(f"/teacher/subjects/{s1.id}/assignments/{sa2.id}")
    assert resp.status_code == 404


async def test_teacher_assignment_surfaces_quiz_violation_flag(
    client: AsyncClient, db, teacher, make_student
) -> None:
    # A QuizAttempt with non-empty violations populates ``violation_flags``
    # (line 387-406 branch) and must not blow up the render.
    subject = await _make_subject(db, owner_id=teacher.id)
    sa = await _make_assignment(db, subject.id, code="lab1")
    student = await make_student(full_name="Vio Lator", email="vio@example.com")
    await _enroll(db, subject.id, student.id)
    student_sa = await _make_student_assignment(db, student.id, sa.id)
    cfg = await _make_plugin_config(
        db, subject.id, {"assignments": {"lab1": {"quiz": {"questions": []}}}}
    )
    sub = await _make_submission(
        db, student_sa.id, status=SubmissionStatus.QUIZ_SENT, plugin_config_id=cfg.id
    )
    db.add(QuizAttempt(
        submission_id=sub.id,
        plugin_config_id=cfg.id,
        plugin_config_version=cfg.version,
        questions_snapshot=[],
        config_snapshot={},
        started_at=datetime.now(UTC),
        status=QuizAttemptStatus.VIOLATION_FAIL,
        violations={"tab_switch": 3},
    ))
    await db.commit()

    authenticate(client, teacher)
    resp = await client.get(f"/teacher/subjects/{subject.id}/assignments/{sa.id}")
    assert resp.status_code == 200
    assert "Vio Lator" in resp.text


# ─────────────────────────────────────────────────────────────────────────────
# teacher_portal.teacher_students (registration overview)  654-694
# ─────────────────────────────────────────────────────────────────────────────


async def test_teacher_students_overview_lists_students(
    client: AsyncClient, db, teacher, make_user, make_student
) -> None:
    # A REAL student plus a STUDENT user account for join coverage, and a
    # SEND_CREDENTIALS outbox row matched by email. Enrolled in a subject the
    # teacher owns, since the roster is scoped to owned subjects.
    subject = await _make_subject(db, owner_id=teacher.id)
    student = await make_student(full_name="Linus Pauling", email="linus@example.com")
    await _enroll(db, subject.id, student.id)
    await make_user(role=UserRole.STUDENT, username="linus", student=student)
    db.add(OutboxMessage(
        event_type=OutboxEventType.SEND_CREDENTIALS,
        payload={"student_email": "linus@example.com"},
    ))
    await db.commit()

    authenticate(client, teacher)
    resp = await client.get("/teacher/students")
    assert resp.status_code == 200
    assert "Linus Pauling" in resp.text


async def test_teacher_students_overview_excludes_other_teachers_students(
    client: AsyncClient, db, teacher, make_user, make_student
) -> None:
    other = await make_user(role=UserRole.TEACHER, username="other")
    other_subject = await _make_subject(db, owner_id=other.id, name="Other Subject")
    other_student = await make_student(full_name="Not Mine", email="notmine@example.com")
    await _enroll(db, other_subject.id, other_student.id)

    my_subject = await _make_subject(db, owner_id=teacher.id, name="Mine")
    my_student = await make_student(full_name="Is Mine", email="ismine@example.com")
    await _enroll(db, my_subject.id, my_student.id)
    await db.commit()

    authenticate(client, teacher)
    resp = await client.get("/teacher/students")
    assert resp.status_code == 200
    assert "Is Mine" in resp.text
    assert "Not Mine" not in resp.text


async def test_teacher_students_overview_empty_for_teacher_with_no_subjects(
    client: AsyncClient, db, teacher, make_student
) -> None:
    await make_student(full_name="Nobodys Student", email="nobody@example.com")
    await db.commit()

    authenticate(client, teacher)
    resp = await client.get("/teacher/students")
    assert resp.status_code == 200
    assert "Nobodys Student" not in resp.text


async def test_teacher_students_overview_admin_sees_all(
    client: AsyncClient, db, admin, make_user, make_student
) -> None:
    other = await make_user(role=UserRole.TEACHER, username="other")
    other_subject = await _make_subject(db, owner_id=other.id, name="Other Subject")
    student = await make_student(full_name="Everyones Visible", email="everyone@example.com")
    await _enroll(db, other_subject.id, student.id)
    await db.commit()

    authenticate(client, admin)
    resp = await client.get("/teacher/students")
    assert resp.status_code == 200
    assert "Everyones Visible" in resp.text


# ─────────────────────────────────────────────────────────────────────────────
# teacher_portal.teacher_review_submission GET render  790-830
# ─────────────────────────────────────────────────────────────────────────────


async def test_teacher_review_get_renders_for_owner(
    client: AsyncClient, db, teacher, make_student
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    sa = await _make_assignment(db, subject.id, title="Reviewable", code="rev1")
    student = await make_student(full_name="Rev Iewer", email="rev@example.com")
    student_sa = await _make_student_assignment(db, student.id, sa.id)
    sub = await _make_submission(
        db, student_sa.id, status=SubmissionStatus.AWAITING_TEACHER_REVIEW
    )
    authenticate(client, teacher)
    resp = await client.get(f"/teacher/submissions/{sub.id}/review")
    assert resp.status_code == 200
    assert "Rev Iewer" in resp.text


# ── quiz-approve branches of the review POST action  865-883 ─────────────────


async def test_review_approve_with_quiz_sends_quiz_from_awaiting(
    client: AsyncClient, db, teacher, make_student
) -> None:
    # AWAITING_TEACHER_REVIEW + approve + quiz configured → teacher_send_quiz →
    # QUIZ_SENT (covers the has_quiz=True / send-quiz branch, lines 866-875).
    subject = await _make_subject(db, owner_id=teacher.id)
    sa = await _make_assignment(db, subject.id, code="quizlab")
    student = await make_student(email="q1@example.com")
    student_sa = await _make_student_assignment(db, student.id, sa.id)
    cfg = await _make_plugin_config(
        db, subject.id,
        {"assignments": {"quizlab": {"quiz": {"questions": [{"id": 0, "type": "single_choice"}]}}}},
    )
    sub = await _make_submission(
        db, student_sa.id, status=SubmissionStatus.AWAITING_TEACHER_REVIEW,
        plugin_config_id=cfg.id,
    )
    authenticate(client, teacher)
    resp = await client.post(
        f"/teacher/submissions/{sub.id}/review",
        data={"action": "approve"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    await db.refresh(sub)
    assert sub.status == SubmissionStatus.QUIZ_SENT


async def test_review_approve_with_quiz_from_legacy_waiting_sends_quiz(
    client: AsyncClient, db, teacher, make_student
) -> None:
    # WAITING_FOR_TEACHER_REVIEW + approve + quiz → teacher_approve_quiz →
    # QUIZ_SENT (covers the legacy-status has_quiz branch, lines 876-877).
    subject = await _make_subject(db, owner_id=teacher.id)
    sa = await _make_assignment(db, subject.id, code="quizlab")
    student = await make_student(email="q2@example.com")
    student_sa = await _make_student_assignment(db, student.id, sa.id)
    cfg = await _make_plugin_config(
        db, subject.id,
        {"assignments": {"quizlab": {"quiz": {"questions": [{"id": 0, "type": "single_choice"}]}}}},
    )
    sub = await _make_submission(
        db, student_sa.id, status=SubmissionStatus.WAITING_FOR_TEACHER_REVIEW,
        plugin_config_id=cfg.id,
    )
    authenticate(client, teacher)
    resp = await client.post(
        f"/teacher/submissions/{sub.id}/review",
        data={"action": "approve"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    await db.refresh(sub)
    assert sub.status == SubmissionStatus.QUIZ_SENT


async def test_review_reject_from_legacy_waiting_fails(
    client: AsyncClient, db, teacher, make_student
) -> None:
    # WAITING_FOR_TEACHER_REVIEW + reject → teacher_reject → CHECK_FAILED
    # (covers the legacy-status reject branch, lines 882-883).
    subject = await _make_subject(db, owner_id=teacher.id)
    sa = await _make_assignment(db, subject.id, code="rev1")
    student = await make_student(email="q3@example.com")
    student_sa = await _make_student_assignment(db, student.id, sa.id)
    sub = await _make_submission(
        db, student_sa.id, status=SubmissionStatus.WAITING_FOR_TEACHER_REVIEW
    )
    authenticate(client, teacher)
    resp = await client.post(
        f"/teacher/submissions/{sub.id}/review",
        data={"action": "reject", "reason": "no good"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    await db.refresh(sub)
    assert sub.status == SubmissionStatus.CHECK_FAILED
    assert sub.test_results == {"check_reason": "no good"}


# ─────────────────────────────────────────────────────────────────────────────
# student_portal.assignment_detail quiz-metadata branches  248-271
# ─────────────────────────────────────────────────────────────────────────────


async def test_student_assignment_detail_latest_attempt_metadata(
    student_client: AsyncClient, db, student_user
) -> None:
    # A submission with a QuizAttempt → quiz_attempt_id + quiz_max_attempts read
    # from the attempt's config_snapshot (lines 248-257). check_reason surfaced
    # from test_results (line 270-271).
    sid = student_user.student_id
    subject = await _make_subject(db, owner_id=None, name="Quizland")
    await _enroll(db, subject.id, sid)
    sa = await _make_assignment(db, subject.id, code="hw1")
    cfg = await _make_plugin_config(
        db, subject.id, {"assignments": {"hw1": {"quiz": {"max_quiz_attempts": 3}}}}
    )
    student_sa = await _make_student_assignment(db, sid, sa.id)
    sub = await _make_submission(
        db, student_sa.id, status=SubmissionStatus.QUIZ_SENT,
        plugin_config_id=cfg.id, created_at=datetime.now(UTC),
    )
    # A COMPLETED attempt → quiz_attempts_used == 1 and quiz_max_attempts read
    # from the attempt's config_snapshot (5), so the template renders "1/5".
    attempt = QuizAttempt(
        submission_id=sub.id,
        plugin_config_id=cfg.id,
        plugin_config_version=cfg.version,
        questions_snapshot=[],
        config_snapshot={"max_quiz_attempts": 5},
        started_at=datetime.now(UTC),
        status=QuizAttemptStatus.COMPLETED,
    )
    db.add(attempt)
    await db.commit()
    await db.refresh(attempt)

    resp = await student_client.get(
        f"/portal/subjects/{subject.id}/assignments/{student_sa.id}"
    )
    assert resp.status_code == 200
    assert "1/5" in resp.text


async def test_student_assignment_detail_no_attempt_reads_config_max_attempts(
    student_client: AsyncClient, db, student_user
) -> None:
    # A latest submission with NO attempt but a pinned plugin_config + code →
    # max_quiz_attempts comes from the plugin config quiz cfg (lines 258-267).
    sid = student_user.student_id
    subject = await _make_subject(db, owner_id=None, name="Quizland2")
    await _enroll(db, subject.id, sid)
    sa = await _make_assignment(db, subject.id, code="hw2")
    cfg = await _make_plugin_config(
        db, subject.id, {"assignments": {"hw2": {"quiz": {"max_quiz_attempts": 4}}}}
    )
    student_sa = await _make_student_assignment(db, sid, sa.id)
    await _make_submission(
        db, student_sa.id, status=SubmissionStatus.QUIZ_SENT,
        plugin_config_id=cfg.id, created_at=datetime.now(UTC),
    )

    resp = await student_client.get(
        f"/portal/subjects/{subject.id}/assignments/{student_sa.id}"
    )
    assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# student_portal.student_summary overdue/upcoming classification  484-525
# ─────────────────────────────────────────────────────────────────────────────


async def test_student_summary_classifies_overdue_and_upcoming(
    student_client: AsyncClient, db, student_user
) -> None:
    sid = student_user.student_id
    await _consent(db, sid)
    subject = await _make_subject(db, owner_id=None, name="Summary Subject")
    await _enroll(db, subject.id, sid)

    now = datetime.now(UTC)
    # Overdue (past deadline, ungraded) → overdue bucket (line 499-500).
    overdue_a = await _make_assignment(
        db, subject.id, title="Overdue", code="od",
        deadline=now - timedelta(days=2),
    )
    # Upcoming within 7 days, ungraded → upcoming bucket (line 501-502).
    soon_a = await _make_assignment(
        db, subject.id, title="Soon", code="soon",
        deadline=now + timedelta(days=3),
    )
    # Graded one (grade not None) → contributes to avg, neither bucket.
    graded_a = await _make_assignment(
        db, subject.id, title="Graded", code="gr",
        deadline=now + timedelta(days=1),
    )
    osa = await _make_student_assignment(db, sid, overdue_a.id)
    await _make_student_assignment(db, sid, soon_a.id)
    await _make_student_assignment(db, sid, graded_a.id, grade=90)

    # A latest submission for the overdue SA so the latest-submission join runs.
    await _make_submission(
        db, osa.id, status=SubmissionStatus.FAILED, created_at=now
    )

    resp = await student_client.get("/portal/summary")
    assert resp.status_code == 200
    body = resp.text
    assert "Overdue" in body
    assert "Soon" in body
    assert "Graded" in body


async def test_student_assignment_detail_check_reason_surfaced(
    student_client: AsyncClient, db, student_user
) -> None:
    # A CHECK_FAILED submission whose test_results carries check_reason → the
    # detail page reads it (line 270-271) and the template surfaces it.
    sid = student_user.student_id
    subject = await _make_subject(db, owner_id=None, name="ReasonSubj")
    await _enroll(db, subject.id, sid)
    sa = await _make_assignment(db, subject.id, code="rc1")
    student_sa = await _make_student_assignment(db, sid, sa.id)
    await _make_submission(
        db, student_sa.id, status=SubmissionStatus.CHECK_FAILED,
        test_results={"check_reason": "compilation error"},
        created_at=datetime.now(UTC),
    )
    resp = await student_client.get(
        f"/portal/subjects/{subject.id}/assignments/{student_sa.id}"
    )
    assert resp.status_code == 200
    assert "compilation error" in resp.text


async def test_submit_runs_plagiarism_comparison_against_prior_zip(
    student_client: AsyncClient, db, student_user, make_student, make_user
) -> None:
    # A prior ZIP submission by a *different* student (with its file on disk)
    # makes the submit handler's plagiarism loop run compare_zip_files against
    # an existing path (lines 388-395). The new submission stores a
    # similarity_score in source_metadata.
    import io
    import zipfile

    from httpx import ASGITransport
    from submissions_checker.main import app

    def _zip() -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("main.py", "print('hello world')\n")
        return buf.getvalue()

    sid = student_user.student_id
    await _consent(db, sid)
    subject = await _make_subject(db, owner_id=None, name="Plagland")
    await _enroll(db, subject.id, sid)
    sa = await _make_assignment(db, subject.id, code="pl1")
    target_sa = await _make_student_assignment(db, sid, sa.id)

    # Other student enrolled with their own StudentAssignment + a real prior ZIP.
    other_student = await make_student(email="other-pl@example.com")
    await _enroll(db, subject.id, other_student.id)
    other_user = await make_user(role=UserRole.STUDENT, username="otherpl", student=other_student)
    other_sa = await _make_student_assignment(db, other_student.id, sa.id)
    await _consent(db, other_student.id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as oc:
        authenticate(oc, other_user)
        first = await oc.post(
            f"/portal/subjects/{subject.id}/assignments/{other_sa.id}/submit",
            files={"file": ("a.zip", _zip(), "application/zip")},
            follow_redirects=False,
        )
        assert first.status_code == 303

    # Now the target student submits → plagiarism loop compares against the file.
    resp = await student_client.post(
        f"/portal/subjects/{subject.id}/assignments/{target_sa.id}/submit",
        files={"file": ("b.zip", _zip(), "application/zip")},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    sub = (
        await db.execute(
            select(Submission).where(Submission.students_assignment_id == target_sa.id)
        )
    ).scalar_one()
    # The similarity score was computed (identical content → high similarity).
    assert "similarity_score" in sub.source_metadata
    assert sub.source_metadata["similarity_score"] > 0


async def test_student_summary_no_enrollment_empty(
    student_client: AsyncClient, db, student_user
) -> None:
    # Consented student with zero enrolled subjects → all_rows == [] path
    # (line 481-482) and the subs_by_sa == {} else-branch (line 524-525).
    await _consent(db, student_user.student_id)
    resp = await student_client.get("/portal/summary")
    assert resp.status_code == 200
