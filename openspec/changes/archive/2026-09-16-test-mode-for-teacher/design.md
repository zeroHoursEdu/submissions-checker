## Context

Teachers have no safe sandbox to verify that subjects and assignments are configured correctly. The only way to test end-to-end is to use a real student account, which pollutes statistics and submission history. This change adds a per-subject "test student" that teachers can impersonate to do a dry run of their own assignments.

Current relevant models:
- `users` — auth accounts; role `STUDENT` requires a linked `students` row.
- `students` — student profile; belongs to a `groups` row.
- `SubjectsStudents` — enrollment M:M join table.
- Auth uses a JWT cookie (`access_token`); `create_access_token(user_id, username, role)` issues tokens.

## Goals / Non-Goals

**Goals:**
- One test student per subject, provisioned on demand by the subject owner.
- Teacher can enter the system as that test student with a single click.
- Test students are systematically excluded from all statistics and counts.
- Credentials (username + plain password) are visible on demand on the subject detail page.

**Non-Goals:**
- Multiple test students per subject.
- Test student appears in enrollment CSV exports.
- Audit trail of teacher impersonation (out of scope for V1).
- Test student receives notifications or feedback requests.

## Decisions

### 1. `type` column lives on `students`, not `users`

Statistics and enrollment queries filter on the `students` table. Adding `type VARCHAR(10) NOT NULL DEFAULT 'REAL'` (CHECK in `REAL`, `TEST`) there keeps all stat exclusions in a single join predicate (`Student.type = 'REAL'`). The corresponding `users` row for a test student is a normal STUDENT-role account; no change needed to the `users` table.

**Alternatives considered:**
- Column on `users`: would require joining through `users` in every stat query that currently only touches `students`.
- Separate `test_students` table: unnecessary indirection for a boolean flag.

### 2. `subject_test_students` table tracks the 1:1 link and plain password

A dedicated join table `subject_test_students (id, subject_id UNIQUE FK, student_id FK, plain_password VARCHAR)` is the cleanest place to enforce "max one test student per subject" (via UNIQUE on `subject_id`) and to store the one-time display password.

**Why store plain password?** This is a synthetic account with no real personal data. The teacher must be able to see credentials without a reset flow. Storing a bcrypt hash and then regenerating is extra complexity for no real security gain on a test account. The plain password is short-lived context for the teacher only.

**Alternatives considered:**
- `test_student_id` FK on `subjects`: pollutes the subjects table with test-mode concerns.
- Password email: test students have fake emails; we'd need a special delivery flow.

### 3. `type` column also lives on `groups`; `__TEST__` group seeded in migration

`groups` gets the same `type VARCHAR(10) NOT NULL DEFAULT 'REAL'` column so it's always unambiguous whether a group is a real cohort or a system artifact — no magic string matching on `name = '__TEST__'` needed at query time.

The `__TEST__` group row (with `type = TEST`) is inserted by the Alembic migration rather than lazily at runtime. This is simpler, deterministic, and avoids a conditional INSERT on every provisioning call.

**Alternatives considered:**
- Lazy creation: saves one migration line but adds runtime branching and a potential race condition under concurrent provisioning.
- Filter by `name` prefix: fragile; a `type` column is explicit and self-documenting.

### 4. Teacher impersonation via short-lived JWT swap

`POST /teacher/subjects/{subject_id}/test-student/enter` issues a new JWT for the test student's user account and replaces the `access_token` cookie. The response redirects to `/portal`. The teacher returns to their own session by logging out and back in (standard flow). No "return token" stacking needed for V1.

**Alternatives considered:**
- Dual-cookie session: complex and non-standard.
- Proxy / server-side session store: overkill for a teacher-QA tool.

### 5. Statistics exclusion via `Student.type = 'REAL'` filter

All queries that count or list students must add `.where(Student.type == 'REAL')` (or equivalent join condition). Identified callsites in `teacher_portal.py`:
- Dashboard `enrolled_count` subquery (line ~74).
- Subject detail student list (line ~150).
- Assignment stats rows query (line ~224).

## Risks / Trade-offs

- [Plain password stored in DB] → Accept: test accounts hold no real data; document in code that this is intentional.
- [Teacher loses their session when entering test mode] → Mitigation: show a clear warning in the UI ("You will be logged out of your teacher account"). Teacher re-logs in normally.
- [Forgetting to filter `type = TEST` in future queries] → Mitigation: add a note to `Student` model docstring; spec requires it; code review.
- [Test student receives notifications] → Mitigation: test students have a fake email (`test+{subject_id}@test.internal`) that can never receive real mail; notification dispatcher will silently fail for unknown domains.

## Migration Plan

1. Add Alembic migration: `groups.type` column + `students.type` column + `subject_test_students` table + INSERT of the `__TEST__` group row.
2. Backfill: all existing students default to `REAL` (server_default handles it).
3. Deploy: no downtime; column is additive, all existing queries still work.
4. Rollback: drop `subject_test_students`, drop `students.type` column, drop `groups.type` column, delete `__TEST__` group row — no data loss for real students or groups.
