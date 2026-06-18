"""Teacher analytics routes — performance and anti-cheating reports."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import and_, func, select, text

from submissions_checker.api.dependencies import AdminUser, DBSession, TeacherUser
from submissions_checker.db.models.enums import UserRole
from submissions_checker.db.models import (
    Group,
    Student,
    StudentAssignment,
    Subject,
    SubjectsAssignment,
    SubjectsStudents,
    Submission,
    User,
    UserLogin,
)
from submissions_checker.core.templates import render

router = APIRouter(prefix="/teacher/analytics", tags=["analytics"])

_LOW_LOGIN_THRESHOLD = 3
_HIGH_GRADE_THRESHOLD = 75


@router.get("", response_class=HTMLResponse)
async def analytics_dashboard(
    request: Request,
    db: DBSession,
    # TODO security: this dashboard aggregates across ALL teachers' subjects.
    # Gated to ADMIN until queries are scoped to subjects owned by the teacher.
    current_user: AdminUser,
) -> HTMLResponse:
    # --- Platform overview scalars ---
    total_students_result = await db.execute(
        select(func.count(func.distinct(SubjectsStudents.student_id)))
    )
    total_students: int = total_students_result.scalar_one() or 0

    active_subjects_result = await db.execute(select(func.count(Subject.id)))
    active_subjects: int = active_subjects_result.scalar_one() or 0

    avg_grade_result = await db.execute(
        select(func.round(func.avg(StudentAssignment.grade), 1)).where(
            StudentAssignment.grade.is_not(None)
        )
    )
    avg_grade: float | None = avg_grade_result.scalar_one_or_none()

    pass_rate_result = await db.execute(
        select(
            func.count()
            .filter(StudentAssignment.grade >= SubjectsAssignment.min_grade)
            .label("passed"),
            func.count(StudentAssignment.grade).label("graded"),
        )
        .join(
            SubjectsAssignment,
            SubjectsAssignment.id == StudentAssignment.subjects_assignment_id,
        )
        .where(StudentAssignment.grade.is_not(None))
    )
    pass_row = pass_rate_result.one()
    pass_rate: float | None = (
        round(pass_row.passed / pass_row.graded * 100, 1) if pass_row.graded else None
    )

    # --- Per-subject stats ---
    subject_stats_result = await db.execute(
        select(
            Subject.id,
            Subject.name,
            func.count(func.distinct(SubjectsStudents.student_id)).label("enrolled_count"),
            func.count(StudentAssignment.id)
            .filter(StudentAssignment.grade.is_not(None))
            .label("graded_count"),
            func.round(func.avg(StudentAssignment.grade), 1).label("avg_grade"),
            func.count(func.distinct(SubjectsAssignment.id)).label("total_assignments"),
        )
        .outerjoin(SubjectsStudents, SubjectsStudents.subject_id == Subject.id)
        .outerjoin(SubjectsAssignment, SubjectsAssignment.subject_id == Subject.id)
        .outerjoin(
            StudentAssignment,
            StudentAssignment.subjects_assignment_id == SubjectsAssignment.id,
        )
        .group_by(Subject.id, Subject.name)
        .order_by(Subject.name)
    )
    subject_stats = [row._asdict() for row in subject_stats_result]

    # --- Assignment difficulty ---
    # Subquery: distinct student_assignment IDs that have at least one submission
    submitted_sq = (
        select(func.distinct(Submission.students_assignment_id).label("sa_id")).subquery()
    )

    difficulty_result = await db.execute(
        select(
            SubjectsAssignment.id,
            SubjectsAssignment.title,
            Subject.name.label("subject_name"),
            SubjectsAssignment.max_grade,
            SubjectsAssignment.min_grade,
            func.count(func.distinct(SubjectsStudents.student_id)).label("enrolled"),
            func.count(func.distinct(submitted_sq.c.sa_id)).label("submitted"),
            func.round(func.avg(StudentAssignment.grade), 1).label("avg_grade"),
            func.min(StudentAssignment.grade).label("min_achieved"),
            func.max(StudentAssignment.grade).label("max_achieved"),
        )
        .join(Subject, Subject.id == SubjectsAssignment.subject_id)
        .join(SubjectsStudents, SubjectsStudents.subject_id == SubjectsAssignment.subject_id)
        .outerjoin(
            StudentAssignment,
            StudentAssignment.subjects_assignment_id == SubjectsAssignment.id,
        )
        .outerjoin(submitted_sq, submitted_sq.c.sa_id == StudentAssignment.id)
        .group_by(
            SubjectsAssignment.id,
            SubjectsAssignment.title,
            SubjectsAssignment.max_grade,
            SubjectsAssignment.min_grade,
            Subject.name,
        )
        .order_by(func.avg(StudentAssignment.grade).asc().nullslast())
    )
    difficulty_rows = [row._asdict() for row in difficulty_result]

    # --- Grade distribution (10-point buckets) ---
    grade_dist_result = await db.execute(
        select(
            (StudentAssignment.grade / 10).label("bucket"),
            func.count().label("count"),
        )
        .where(StudentAssignment.grade.is_not(None))
        .group_by((StudentAssignment.grade / 10))
        .order_by((StudentAssignment.grade / 10))
    )
    grade_distribution = {row.bucket: row.count for row in grade_dist_result}
    grade_dist_labels = [f"{b * 10}–{b * 10 + 9 if b < 10 else 100}" for b in range(11)]
    grade_dist_data = [grade_distribution.get(b, 0) for b in range(11)]

    # --- Quiz failures: students who exhausted all attempts with no pass ---
    quiz_failures_result = await db.execute(
        text("""
            SELECT
                s.id            AS student_id,
                s.full_name     AS student_name,
                g.name          AS group_name,
                sa.title        AS assignment_title,
                subj.name       AS subject_name,
                MAX((qa.config_snapshot->>'max_quiz_attempts')::int) AS max_attempts,
                COUNT(qa.id)    AS attempts_used,
                MAX(qa.submitted_at) AS last_attempt_at
            FROM students_assignments stud_asgn
            JOIN subjects_assignments sa   ON sa.id   = stud_asgn.subjects_assignment_id
            JOIN subjects subj             ON subj.id = sa.subject_id
            JOIN students s                ON s.id    = stud_asgn.student_id
            JOIN groups g                  ON g.id    = s.group_id
            JOIN submissions sub           ON sub.students_assignment_id = stud_asgn.id
            JOIN quiz_attempts qa          ON qa.submission_id = sub.id
                                           AND qa.status IN ('COMPLETED', 'TIMED_OUT')
            WHERE (qa.config_snapshot->>'max_quiz_attempts') IS NOT NULL
            GROUP BY s.id, s.full_name, g.name, sa.title, subj.name, stud_asgn.id
            HAVING COUNT(qa.id) >= MAX((qa.config_snapshot->>'max_quiz_attempts')::int)
               AND bool_and(qa.is_passed IS NOT TRUE)
            ORDER BY subj.name, sa.title, s.full_name
        """)
    )
    quiz_failures = [dict(row._mapping) for row in quiz_failures_result]
    quiz_failures_count = len(quiz_failures)

    return render(request, "analytics_dashboard.html", {
            "current_user": current_user,
            "total_students": total_students,
            "active_subjects": active_subjects,
            "avg_grade": avg_grade,
            "pass_rate": pass_rate,
            "subject_stats": subject_stats,
            "difficulty_rows": difficulty_rows,
            "grade_dist_labels": grade_dist_labels,
            "grade_dist_data": grade_dist_data,
            "quiz_failures": quiz_failures,
            "quiz_failures_count": quiz_failures_count,
        })


@router.get("/fraud", response_class=HTMLResponse)
async def analytics_fraud(
    request: Request,
    db: DBSession,
    # TODO security: this report aggregates across ALL teachers' subjects/students.
    # Gated to ADMIN until queries are scoped to subjects owned by the teacher.
    current_user: AdminUser,
) -> HTMLResponse:
    # Reusable subquery: login stats per user
    login_stats_sq = (
        select(
            UserLogin.user_id,
            func.count(UserLogin.id).label("login_count"),
            func.min(UserLogin.logged_in_at).label("first_login"),
            func.max(UserLogin.logged_in_at).label("last_login"),
        )
        .group_by(UserLogin.user_id)
        .subquery()
    )

    # --- Flag 1: First login within 24h before deadline + high grade (>= 80% of max) ---
    late_login_result = await db.execute(
        select(
            Student.id.label("student_id"),
            Student.full_name,
            Group.name.label("group_name"),
            SubjectsAssignment.title.label("assignment_title"),
            SubjectsAssignment.deadline,
            StudentAssignment.grade,
            SubjectsAssignment.max_grade,
            login_stats_sq.c.first_login,
            login_stats_sq.c.login_count,
        )
        .join(User, User.student_id == Student.id)
        .join(login_stats_sq, login_stats_sq.c.user_id == User.id)
        .join(StudentAssignment, StudentAssignment.student_id == Student.id)
        .join(
            SubjectsAssignment,
            SubjectsAssignment.id == StudentAssignment.subjects_assignment_id,
        )
        .join(Group, Group.id == Student.group_id)
        .where(
            SubjectsAssignment.deadline.is_not(None),
            StudentAssignment.grade.is_not(None),
            login_stats_sq.c.first_login
            >= SubjectsAssignment.deadline - text("INTERVAL '24 hours'"),
            login_stats_sq.c.first_login <= SubjectsAssignment.deadline,
            StudentAssignment.grade >= SubjectsAssignment.max_grade * 0.8,
        )
        .order_by(Student.full_name)
    )
    late_login_flags = [row._asdict() for row in late_login_result]

    # --- Flag 2: Few total logins + high average grade ---
    few_logins_result = await db.execute(
        select(
            Student.id.label("student_id"),
            Student.full_name,
            Group.name.label("group_name"),
            login_stats_sq.c.login_count,
            login_stats_sq.c.first_login,
            login_stats_sq.c.last_login,
            func.round(func.avg(StudentAssignment.grade), 1).label("avg_grade"),
            func.count(StudentAssignment.grade).label("graded_count"),
        )
        .join(User, User.student_id == Student.id)
        .join(login_stats_sq, login_stats_sq.c.user_id == User.id)
        .join(StudentAssignment, StudentAssignment.student_id == Student.id)
        .join(Group, Group.id == Student.group_id)
        .where(
            login_stats_sq.c.login_count < _LOW_LOGIN_THRESHOLD,
            StudentAssignment.grade.is_not(None),
        )
        .group_by(
            Student.id,
            Student.full_name,
            Group.name,
            login_stats_sq.c.login_count,
            login_stats_sq.c.first_login,
            login_stats_sq.c.last_login,
        )
        .having(func.avg(StudentAssignment.grade) >= _HIGH_GRADE_THRESHOLD)
        .order_by(login_stats_sq.c.login_count.asc(), Student.full_name)
    )
    few_logins_flags = [row._asdict() for row in few_logins_result]

    # --- Flag 3: 3+ submissions all on the same calendar day ---
    single_day_result = await db.execute(
        select(
            Student.id.label("student_id"),
            Student.full_name,
            Group.name.label("group_name"),
            func.count(Submission.id).label("total_submissions"),
            func.min(func.cast(Submission.created_at, type_=None)).label("earliest_day"),
        )
        .join(StudentAssignment, StudentAssignment.student_id == Student.id)
        .join(Submission, Submission.students_assignment_id == StudentAssignment.id)
        .join(Group, Group.id == Student.group_id)
        .group_by(Student.id, Student.full_name, Group.name)
        .having(
            func.count(Submission.id) >= 3,
            func.min(func.date(Submission.created_at))
            == func.max(func.date(Submission.created_at)),
        )
        .order_by(func.count(Submission.id).desc())
    )
    single_day_flags = [row._asdict() for row in single_day_result]

    # --- Login activity overview (all students) ---
    login_activity_result = await db.execute(
        select(
            Student.id.label("student_id"),
            Student.full_name,
            Group.name.label("group_name"),
            func.count(UserLogin.id).label("login_count"),
            func.min(UserLogin.logged_in_at).label("first_login"),
            func.max(UserLogin.logged_in_at).label("last_login"),
        )
        .join(User, User.student_id == Student.id)
        .join(Group, Group.id == Student.group_id)
        .outerjoin(UserLogin, UserLogin.user_id == User.id)
        .group_by(Student.id, Student.full_name, Group.name)
        .order_by(func.count(UserLogin.id).asc(), Student.full_name)
    )
    login_activity = [row._asdict() for row in login_activity_result]

    # Assemble risk scores from flags
    risk_scores: dict[int, int] = {}
    for row in late_login_flags:
        risk_scores[row["student_id"]] = risk_scores.get(row["student_id"], 0) + 3
    for row in few_logins_flags:
        risk_scores[row["student_id"]] = risk_scores.get(row["student_id"], 0) + 2
    for row in single_day_flags:
        risk_scores[row["student_id"]] = risk_scores.get(row["student_id"], 0) + 2

    return render(request, "analytics_fraud.html", {
            "current_user": current_user,
            "late_login_flags": late_login_flags,
            "few_logins_flags": few_logins_flags,
            "single_day_flags": single_day_flags,
            "login_activity": login_activity,
            "risk_scores": risk_scores,
            "low_login_threshold": _LOW_LOGIN_THRESHOLD,
            "high_grade_threshold": _HIGH_GRADE_THRESHOLD,
        })


@router.get("/students/{student_id}", response_class=HTMLResponse)
async def analytics_student(
    request: Request,
    student_id: int,
    db: DBSession,
    current_user: TeacherUser,
) -> HTMLResponse:
    # Student + group
    student_result = await db.execute(
        select(Student, Group.name.label("group_name"))
        .join(Group, Group.id == Student.group_id)
        .where(Student.id == student_id)
    )
    row = student_result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Student not found")
    student = row.Student
    group_name = row.group_name

    # Object-level authz: a non-admin teacher may only view a student who is
    # enrolled in at least one subject they own.
    if current_user.role != UserRole.ADMIN:
        owned_enrollment = await db.execute(
            select(SubjectsStudents.student_id)
            .join(Subject, Subject.id == SubjectsStudents.subject_id)
            .where(
                SubjectsStudents.student_id == student_id,
                Subject.owner_id == current_user.user_id,
            )
            .limit(1)
        )
        if owned_enrollment.scalar_one_or_none() is None:
            raise HTTPException(status_code=403, detail="Not authorized for this student")

    # Per-subject breakdown for this student
    subject_profile_result = await db.execute(
        select(
            Subject.id.label("subject_id"),
            Subject.name.label("subject_name"),
            SubjectsStudents.enrolled_at,
            func.count(SubjectsAssignment.id).label("total_assignments"),
            func.count(StudentAssignment.grade).label("graded_count"),
            func.round(func.avg(StudentAssignment.grade), 1).label("avg_grade"),
            func.count(Submission.id).label("submission_count"),
        )
        .join(
            SubjectsStudents,
            and_(
                SubjectsStudents.subject_id == Subject.id,
                SubjectsStudents.student_id == student_id,
            ),
        )
        .outerjoin(SubjectsAssignment, SubjectsAssignment.subject_id == Subject.id)
        .outerjoin(
            StudentAssignment,
            and_(
                StudentAssignment.subjects_assignment_id == SubjectsAssignment.id,
                StudentAssignment.student_id == student_id,
            ),
        )
        .outerjoin(Submission, Submission.students_assignment_id == StudentAssignment.id)
        .group_by(Subject.id, Subject.name, SubjectsStudents.enrolled_at)
        .order_by(Subject.name)
    )
    subject_profiles = [row._asdict() for row in subject_profile_result]

    # Grade timeline (graded assignments ordered by deadline)
    timeline_result = await db.execute(
        select(
            SubjectsAssignment.title.label("assignment_title"),
            Subject.name.label("subject_name"),
            SubjectsAssignment.deadline,
            SubjectsAssignment.min_grade,
            SubjectsAssignment.max_grade,
            StudentAssignment.grade,
        )
        .join(
            SubjectsAssignment,
            SubjectsAssignment.id == StudentAssignment.subjects_assignment_id,
        )
        .join(Subject, Subject.id == SubjectsAssignment.subject_id)
        .where(
            StudentAssignment.student_id == student_id,
            StudentAssignment.grade.is_not(None),
        )
        .order_by(SubjectsAssignment.deadline.asc().nullslast())
    )
    grade_timeline = [row._asdict() for row in timeline_result]

    # All assignments (graded or not)
    all_assignments_result = await db.execute(
        select(
            SubjectsAssignment.title.label("assignment_title"),
            Subject.name.label("subject_name"),
            SubjectsAssignment.deadline,
            SubjectsAssignment.min_grade,
            SubjectsAssignment.max_grade,
            StudentAssignment.grade,
        )
        .join(
            SubjectsAssignment,
            SubjectsAssignment.id == StudentAssignment.subjects_assignment_id,
        )
        .join(Subject, Subject.id == SubjectsAssignment.subject_id)
        .where(StudentAssignment.student_id == student_id)
        .order_by(Subject.name, SubjectsAssignment.deadline.asc().nullslast())
    )
    all_assignments = [row._asdict() for row in all_assignments_result]

    # Login stats
    login_stats_result = await db.execute(
        select(
            func.count(UserLogin.id).label("login_count"),
            func.min(UserLogin.logged_in_at).label("first_login"),
            func.max(UserLogin.logged_in_at).label("last_login"),
        )
        .join(User, User.id == UserLogin.user_id)
        .where(User.student_id == student_id)
    )
    login_stats = login_stats_result.one()

    timeline_labels = [r["assignment_title"] for r in grade_timeline]
    timeline_grades = [r["grade"] for r in grade_timeline]
    timeline_min = [r["min_grade"] for r in grade_timeline]
    timeline_max = [r["max_grade"] for r in grade_timeline]

    return render(request, "analytics_student.html", {
            "current_user": current_user,
            "student": student,
            "group_name": group_name,
            "subject_profiles": subject_profiles,
            "all_assignments": all_assignments,
            "login_stats": login_stats,
            "timeline_labels": timeline_labels,
            "timeline_grades": timeline_grades,
            "timeline_min": timeline_min,
            "timeline_max": timeline_max,
        })
