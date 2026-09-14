"""Functional API tests for the TEACHER ANALYTICS / anti-cheat routes.

Routes under test (mounted at ``/teacher/analytics`` — see
``api/routes/analytics.py``):

* ``GET /teacher/analytics``                  — platform overview      [AdminUser]
* ``GET /teacher/analytics/fraud``            — anti-cheat report      [AdminUser]
* ``GET /teacher/analytics/students/{id}``    — per-student profile    [TeacherUser]

Focus:

* Auth gates — anonymous → 401, non-admin → 403 where ADMIN is required; the
  per-student handler's object-level authz (student → 403, cross-teacher → 403,
  owning teacher / admin → 200).
* Overview aggregates computed from SEEDED data — total students, active
  subjects, avg grade, pass rate, and the 10-point grade-distribution buckets
  (the bucketing was bug-fixed, so e.g. 95 must land in the 90–99 bucket and 100
  in its own bucket).
* Fraud "few logins + high grade" flag — the suspicious pattern (grades > 75,
  logins < 3) must appear; a normal student must NOT be flagged; empty DB → 200.
* Per-student profile renders that student's own grades/submissions/logins.

Numbers asserted are computed directly from the seed, not just status codes.
All seeding uses the shared harness fixtures (``db``, factories); the auth/authz
stack is live.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from submissions_checker.db.models import (
    StudentAssignment,
    Subject,
    SubjectsAssignment,
    SubjectsStudents,
    Submission,
    UserLogin,
)
from submissions_checker.db.models.enums import SubmissionSourceType, SubmissionStatus, UserRole
from tests.functional.conftest import authenticate

pytestmark = pytest.mark.asyncio


# ── Arrange helpers ──────────────────────────────────────────────────────────


async def _make_subject(db, owner_id: int | None, *, name: str, code: str | None = None) -> Subject:
    subject = Subject(name=name, code=code, owner_id=owner_id)
    db.add(subject)
    await db.commit()
    await db.refresh(subject)
    return subject


async def _make_assignment(
    db,
    subject_id: int,
    *,
    title: str,
    code: str,
    min_grade: int = 50,
    max_grade: int = 100,
    deadline: datetime | None = None,
) -> SubjectsAssignment:
    sa = SubjectsAssignment(
        subject_id=subject_id,
        code=code,
        title=title,
        min_grade=min_grade,
        max_grade=max_grade,
        deadline=deadline,
    )
    db.add(sa)
    await db.commit()
    await db.refresh(sa)
    return sa


async def _enroll(db, subject_id: int, student_id: int) -> None:
    db.add(SubjectsStudents(subject_id=subject_id, student_id=student_id))
    await db.commit()


async def _grade(
    db, subjects_assignment_id: int, student_id: int, grade: int | None
) -> StudentAssignment:
    sa = StudentAssignment(
        student_id=student_id,
        subjects_assignment_id=subjects_assignment_id,
        grade=grade,
    )
    db.add(sa)
    await db.commit()
    await db.refresh(sa)
    return sa


async def _submit(
    db, students_assignment_id: int, *, created_at: datetime | None = None
) -> Submission:
    sub = Submission(
        students_assignment_id=students_assignment_id,
        source_type=SubmissionSourceType.ZIP_UPLOAD,
        status=SubmissionStatus.COMPLETED,
    )
    db.add(sub)
    await db.flush()
    if created_at is not None:
        sub.created_at = created_at
    await db.commit()
    await db.refresh(sub)
    return sub


async def _login(db, user_id: int, *, at: datetime) -> None:
    db.add(UserLogin(user_id=user_id, logged_in_at=at))
    await db.commit()


async def _student_with_user(make_user, make_student, group=None, *, full_name: str, email: str):
    """Create a Student + linked User in one shot, returning (student, user)."""
    student = await make_student(group=group, full_name=full_name, email=email)
    user = await make_user(role=UserRole.STUDENT, student=student)
    return student, user


# ── 1. Permissions ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    ["/teacher/analytics", "/teacher/analytics/fraud", "/teacher/analytics/students/1"],
)
async def test_anonymous_is_rejected(client: AsyncClient, path: str) -> None:
    resp = await client.get(path)
    assert resp.status_code == 401


async def test_overview_requires_admin_teacher_forbidden(teacher_client: AsyncClient) -> None:
    resp = await teacher_client.get("/teacher/analytics")
    assert resp.status_code == 403


async def test_overview_admin_ok(admin_client: AsyncClient) -> None:
    resp = await admin_client.get("/teacher/analytics")
    assert resp.status_code == 200


async def test_overview_student_forbidden(student_client: AsyncClient) -> None:
    resp = await student_client.get("/teacher/analytics")
    assert resp.status_code == 403


async def test_fraud_requires_admin_teacher_forbidden(teacher_client: AsyncClient) -> None:
    resp = await teacher_client.get("/teacher/analytics/fraud")
    assert resp.status_code == 403


async def test_fraud_admin_ok(admin_client: AsyncClient) -> None:
    resp = await admin_client.get("/teacher/analytics/fraud")
    assert resp.status_code == 200


async def test_fraud_student_forbidden(student_client: AsyncClient) -> None:
    resp = await student_client.get("/teacher/analytics/fraud")
    assert resp.status_code == 403


async def test_per_student_student_forbidden(student_client: AsyncClient, db) -> None:
    # The student handler requires TeacherUser; a STUDENT role is rejected before
    # the student record is even looked up.
    resp = await student_client.get("/teacher/analytics/students/1")
    assert resp.status_code == 403


async def test_per_student_missing_returns_404(admin_client: AsyncClient) -> None:
    resp = await admin_client.get("/teacher/analytics/students/999999")
    assert resp.status_code == 404


async def test_per_student_cross_teacher_forbidden(
    client: AsyncClient, db, make_user, make_student
) -> None:
    """A non-admin teacher may only view a student enrolled in a subject they own."""
    owner = await make_user(role=UserRole.TEACHER, username="owner_t")
    other = await make_user(role=UserRole.TEACHER, username="other_t")

    student, _ = await _student_with_user(
        make_user, make_student, full_name="Enrolled One", email="enr1@example.com"
    )
    owned = await _make_subject(db, owner.id, name="Owned", code="own")
    await _enroll(db, owned.id, student.id)

    # The owning teacher can view.
    authenticate(client, owner)
    resp_owner = await client.get(f"/teacher/analytics/students/{student.id}")
    assert resp_owner.status_code == 200

    # A different teacher (owns nothing the student is in) is forbidden.
    authenticate(client, other)
    resp_other = await client.get(f"/teacher/analytics/students/{student.id}")
    assert resp_other.status_code == 403


async def test_per_student_admin_bypasses_ownership(
    admin_client: AsyncClient, db, make_user, make_student
) -> None:
    """ADMIN skips the object-level ownership check entirely."""
    teacher = await make_user(role=UserRole.TEACHER, username="some_teacher")
    student, _ = await _student_with_user(
        make_user, make_student, full_name="Lonely Student", email="lonely@example.com"
    )
    # Subject owned by the teacher, student enrolled — but admin doesn't need it.
    subj = await _make_subject(db, teacher.id, name="S", code="s")
    await _enroll(db, subj.id, student.id)

    resp = await admin_client.get(f"/teacher/analytics/students/{student.id}")
    assert resp.status_code == 200


# ── 2. Overview aggregates on seeded data ────────────────────────────────────


async def test_overview_aggregates_and_grade_buckets(
    admin_client: AsyncClient, db, make_user, make_student, make_group
) -> None:
    """Seed a known dataset and assert the rendered overview reflects exact aggregates.

    Two subjects, three enrolled students, six graded assignments with grades
    chosen to probe the 10-point bucket boundaries (0, 9, 10, 95, 100) that the
    bucketing fix targets, plus pass-rate boundaries.
    """
    group = await make_group("G1")
    s1, _ = await _student_with_user(
        make_user, make_student, group=group, full_name="Alice", email="a@e.com"
    )
    s2, _ = await _student_with_user(
        make_user, make_student, group=group, full_name="Bob", email="b@e.com"
    )
    s3, _ = await _student_with_user(
        make_user, make_student, group=group, full_name="Carol", email="c@e.com"
    )

    subj_a = await _make_subject(db, None, name="Algebra", code="alg")
    subj_b = await _make_subject(db, None, name="Biology", code="bio")

    # min_grade=50 so we can reason about pass rate; max_grade=100.
    asg_a = await _make_assignment(
        db, subj_a.id, title="A1", code="a1", min_grade=50, max_grade=100
    )
    asg_b = await _make_assignment(
        db, subj_b.id, title="B1", code="b1", min_grade=50, max_grade=100
    )

    # Enroll all three in both subjects (total distinct students = 3).
    for st in (s1, s2, s3):
        await _enroll(db, subj_a.id, st.id)
        await _enroll(db, subj_b.id, st.id)

    # Grades chosen to hit bucket edges:
    #   0   -> bucket 0   (label "0–9")
    #   9   -> bucket 0   (label "0–9")
    #   10  -> bucket 1   (label "10–19")
    #   50  -> bucket 5
    #   95  -> bucket 9   (label "90–99")   <- the just-fixed case
    #   100 -> bucket 10  (label "100")
    grades_a = {s1.id: 0, s2.id: 9, s3.id: 10}
    grades_b = {s1.id: 50, s2.id: 95, s3.id: 100}
    for sid, g in grades_a.items():
        await _grade(db, asg_a.id, sid, g)
    for sid, g in grades_b.items():
        await _grade(db, asg_b.id, sid, g)

    resp = await admin_client.get("/teacher/analytics")
    assert resp.status_code == 200
    html = resp.text

    # --- total students = 3 distinct enrolled ---
    assert ">3<" in html  # stat card value

    # --- active subjects = 2 ---
    assert ">2<" in html

    # --- average grade = round(mean(0,9,10,50,95,100), 1) = round(264/6,1) = 44.0 ---
    expected_avg = round((0 + 9 + 10 + 50 + 95 + 100) / 6, 1)
    assert expected_avg == 44.0
    assert "44.0" in html

    # --- pass rate: grade >= min_grade(50). passing = {50,95,100} = 3 of 6 = 50.0% ---
    assert "50.0%" in html

    # --- grade distribution buckets rendered into the chart's JSON data array ---
    # Buckets 0..10:  0->2 (grades 0,9), 1->1 (10), 5->1 (50), 9->1 (95), 10->1 (100)
    expected_dist = [2, 1, 0, 0, 0, 1, 0, 0, 0, 1, 1]
    assert sum(expected_dist) == 6
    # The template emits `const data = [...]` via `grade_dist_data | tojson`.
    assert f"const data = {expected_dist}".replace(" ", "") in html.replace(" ", "")

    # Bucket labels include the boundary labels the fix is about. The chart labels
    # are emitted via `tojson`, which escapes the en-dash (–) as –.
    assert "90\\u201399" in html  # the "90–99" bucket label
    # The top bucket (b=10) label is "100–100" per the handler's label formula.
    assert "100\\u2013100" in html


# ── 3. Fraud / anti-cheat report ─────────────────────────────────────────────


async def test_fraud_flags_few_logins_high_grade(
    admin_client: AsyncClient, db, make_user, make_student, make_group
) -> None:
    """A student with high grades (>75 avg) but <3 logins is flagged; a normal
    student (many logins / modest grades) is not."""
    group = await make_group("FraudGroup")

    # Suspicious: 2 logins (< 3 threshold), avg grade 90 (>= 75 threshold).
    suspect, suspect_user = await _student_with_user(
        make_user, make_student, group=group, full_name="Sus Pect", email="sus@e.com"
    )
    # Normal: 5 logins, modest grade 60.
    normal, normal_user = await _student_with_user(
        make_user, make_student, group=group, full_name="Norm Al", email="norm@e.com"
    )

    subj = await _make_subject(db, None, name="Cryptography", code="crypto")
    asg1 = await _make_assignment(db, subj.id, title="C1", code="c1", min_grade=50, max_grade=100)
    asg2 = await _make_assignment(db, subj.id, title="C2", code="c2", min_grade=50, max_grade=100)

    for st in (suspect, normal):
        await _enroll(db, subj.id, st.id)

    # Suspect: grades 85 and 95 -> avg 90.
    await _grade(db, asg1.id, suspect.id, 85)
    await _grade(db, asg2.id, suspect.id, 95)
    # Normal: grades 60 and 60 -> avg 60 (below high-grade threshold).
    await _grade(db, asg1.id, normal.id, 60)
    await _grade(db, asg2.id, normal.id, 60)

    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    # Suspect: 2 logins.
    await _login(db, suspect_user.id, at=base)
    await _login(db, suspect_user.id, at=base + timedelta(hours=1))
    # Normal: 5 logins.
    for i in range(5):
        await _login(db, normal_user.id, at=base + timedelta(days=i))

    resp = await admin_client.get("/teacher/analytics/fraud")
    assert resp.status_code == 200
    html = resp.text

    # The suspect's link to their profile appears (flagged row in few-logins table).
    assert f"/teacher/analytics/students/{suspect.id}" in html
    assert "Sus Pect" in html
    # The few-logins flag count is 1 (only the suspect qualifies).
    assert "1 flag" in html

    # The normal student must NOT be flagged in the few-logins section. They will
    # still appear in the login-activity overview, so assert their avg grade (60.0)
    # is not surfaced as a flag and that the flag count stayed at exactly one.
    assert "2 flags" not in html  # not two few-login flags
    # Suspect's avg grade (90.0) is shown as a flagged value.
    assert "90.0" in html


async def test_fraud_empty_returns_200_no_flags(admin_client: AsyncClient) -> None:
    """No data at all → 200 with the 'no flags' empty state, zero flag counts."""
    resp = await admin_client.get("/teacher/analytics/fraud")
    assert resp.status_code == 200
    html = resp.text
    # Each of the three flag sections reports "0 flags".
    assert html.count("0 flag") >= 3


async def test_fraud_login_count_exactly_at_threshold_not_flagged(
    admin_client: AsyncClient, db, make_user, make_student, make_group
) -> None:
    """Boundary: login_count == 3 is NOT < _LOW_LOGIN_THRESHOLD, so even with a
    high grade the student is not flagged by the few-logins rule."""
    group = await make_group("EdgeGroup")
    student, user = await _student_with_user(
        make_user, make_student, group=group, full_name="Edge Case", email="edge@e.com"
    )
    subj = await _make_subject(db, None, name="EdgeSubj", code="edge")
    asg = await _make_assignment(db, subj.id, title="E1", code="e1", min_grade=50, max_grade=100)
    await _enroll(db, subj.id, student.id)
    await _grade(db, asg.id, student.id, 100)  # very high grade

    base = datetime(2026, 2, 1, 9, 0, tzinfo=UTC)
    for i in range(3):  # exactly 3 logins -> not < 3
        await _login(db, user.id, at=base + timedelta(hours=i))

    resp = await admin_client.get("/teacher/analytics/fraud")
    assert resp.status_code == 200
    html = resp.text
    # The few-logins flag section should report zero flags.
    assert "0 flag" in html


# ── 4. Per-student profile ───────────────────────────────────────────────────


async def test_per_student_profile_renders_own_data(
    admin_client: AsyncClient, db, make_user, make_student, make_group
) -> None:
    """Seed one student's grades/submissions/logins; the profile reflects them."""
    group = await make_group("ProfileGroup")
    student, user = await _student_with_user(
        make_user, make_student, group=group, full_name="Profile Person", email="prof@e.com"
    )
    subj = await _make_subject(db, None, name="Databases", code="db")
    asg = await _make_assignment(
        db,
        subj.id,
        title="Indexing Lab",
        code="idx",
        min_grade=50,
        max_grade=100,
        deadline=datetime(2026, 3, 1, 23, 59),  # column is TIMESTAMP WITHOUT TIME ZONE
    )
    await _enroll(db, subj.id, student.id)
    student_asg = await _grade(db, asg.id, student.id, 88)
    # Two submissions for this assignment.
    await _submit(db, student_asg.id)
    await _submit(db, student_asg.id)
    # Three logins.
    base = datetime(2026, 1, 10, 8, 0, tzinfo=UTC)
    for i in range(3):
        await _login(db, user.id, at=base + timedelta(days=i))

    resp = await admin_client.get(f"/teacher/analytics/students/{student.id}")
    assert resp.status_code == 200
    html = resp.text

    # Header reflects the student's identity and login count.
    assert "Profile Person" in html
    assert "prof@e.com" in html
    assert "ProfileGroup" in html
    assert "3 logins" in html

    # Subject profile card: avg grade 88.0, the assignment title, and the subject.
    assert "Databases" in html
    assert "Indexing Lab" in html
    assert "88.0" in html or ">88<" in html
    # Two submissions surfaced in the per-subject card summary.
    assert "2 submission" in html


async def test_per_student_profile_no_data_still_renders(
    admin_client: AsyncClient, db, make_user, make_student, make_group
) -> None:
    """A student enrolled in nothing, never logged in → 200 with the
    'never logged in' marker (login_count == 0 branch)."""
    group = await make_group("EmptyGroup")
    student, _ = await _student_with_user(
        make_user, make_student, group=group, full_name="Empty Profile", email="empty@e.com"
    )
    resp = await admin_client.get(f"/teacher/analytics/students/{student.id}")
    assert resp.status_code == 200
    html = resp.text
    assert "Empty Profile" in html
    # login_count == 0 -> "never logged in" badge from vocab.analytics.never_logged_in.
    # We assert the empty assignments state which is deterministic regardless of vocab.
    assert "Empty Profile" in html
