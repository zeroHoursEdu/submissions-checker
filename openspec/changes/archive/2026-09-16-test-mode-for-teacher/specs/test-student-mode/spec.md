## ADDED Requirements

### Requirement: Students have a type distinguishing real from test accounts

The `students` table SHALL have a `type` column of type `VARCHAR(10) NOT NULL DEFAULT 'REAL'` with a CHECK constraint allowing only the values `REAL` and `TEST`.

All existing students SHALL default to `REAL`.

#### Scenario: Existing student has type REAL by default
- **WHEN** the migration runs on an existing database
- **THEN** all pre-existing rows in `students` have `type = 'REAL'`

#### Scenario: New real student has type REAL
- **WHEN** a student is created through the normal enrollment CSV flow
- **THEN** `students.type` is `'REAL'`

### Requirement: Groups have a type distinguishing real cohorts from system groups

The `groups` table SHALL have a `type` column of type `VARCHAR(10) NOT NULL DEFAULT 'REAL'` with a CHECK constraint allowing only the values `REAL` and `TEST`.

All existing groups SHALL default to `REAL`.

#### Scenario: Existing group has type REAL by default
- **WHEN** the migration runs on an existing database
- **THEN** all pre-existing rows in `groups` have `type = 'REAL'`

### Requirement: A single shared test group is seeded by the migration

The Alembic migration SHALL insert a `groups` row with `name = '__TEST__'`, `type = 'TEST'`, and `description = 'System group for test students'` if it does not already exist.

All test students SHALL be assigned to this group.

#### Scenario: Test group exists after migration
- **WHEN** the migration runs
- **THEN** a group with `name = '__TEST__'` and `type = 'TEST'` exists in the database

#### Scenario: All test students share the same group
- **WHEN** two teachers each provision a test student for their respective subjects
- **THEN** both test students belong to the same `__TEST__` group row

### Requirement: Each subject may have at most one test student

A `subject_test_students` table SHALL exist with columns:
- `id` BIGINT PK
- `subject_id` BIGINT NOT NULL UNIQUE FK → `subjects.id` ON DELETE CASCADE
- `student_id` BIGINT NOT NULL FK → `students.id` ON DELETE CASCADE
- `plain_password` VARCHAR(128) NOT NULL
- `created_at` TIMESTAMPTZ NOT NULL DEFAULT now()

The UNIQUE constraint on `subject_id` enforces the one-test-student-per-subject invariant at the database level.

#### Scenario: Second test student creation is rejected at DB level
- **WHEN** a `subject_test_students` row already exists for `subject_id = 42`
- **THEN** inserting another row with `subject_id = 42` raises a unique constraint violation

### Requirement: Teacher can provision a test student for a subject

`POST /teacher/subjects/{subject_id}/test-student` SHALL:

1. Check if a `subject_test_students` row exists for the given subject.
2. If it does NOT exist: create a `students` row with `type = 'TEST'`, a corresponding `users` row with role `STUDENT`, enroll the student in the subject via `SubjectsStudents`, and insert a `subject_test_students` row storing the plain password. The username SHALL follow the pattern `test_{subject_id}` and the email `test+{subject_id}@test.internal`.
3. If it DOES exist: do nothing (idempotent).
4. In both cases redirect to `GET /teacher/subjects/{subject_id}` with a query param `test_student=created` or `test_student=existing`.

Only the subject owner (where `subjects.owner_id == current_user.id`) SHALL be permitted to call this endpoint. Non-owners receive HTTP 403.

#### Scenario: Teacher provisions test student for the first time
- **WHEN** the owner POSTs to `/teacher/subjects/5/test-student` and no test student exists yet
- **THEN** a student with `type = 'TEST'` is created, enrolled in subject 5, and the teacher is redirected to the subject page with `test_student=created`

#### Scenario: Teacher requests test student that already exists
- **WHEN** the owner POSTs to `/teacher/subjects/5/test-student` and a test student already exists
- **THEN** no new student is created and the teacher is redirected with `test_student=existing`

#### Scenario: Non-owner cannot provision test student
- **WHEN** a teacher who is NOT the owner POSTs to `/teacher/subjects/5/test-student`
- **THEN** the system returns HTTP 403

### Requirement: Subject detail page shows test student panel

The subject detail page (`GET /teacher/subjects/{subject_id}`) SHALL include a "Test Student" panel that:

- If no test student exists: shows a "Create Test Student" button (POST form to the provisioning endpoint).
- If a test student exists: shows the username, plain password, and an "Enter as Test Student" button.

The panel SHALL only be visible to the subject owner.

#### Scenario: Panel shows create button when no test student exists
- **WHEN** the owner views the subject detail page and no test student has been provisioned
- **THEN** a "Create Test Student" button is visible

#### Scenario: Panel shows credentials when test student exists
- **WHEN** the owner views the subject detail page and a test student exists
- **THEN** the username and plain password are displayed along with an "Enter as Test Student" button

#### Scenario: Panel is hidden for non-owners
- **WHEN** a teacher who is not the owner views the subject detail page
- **THEN** the test student panel is not rendered

### Requirement: Teacher can enter the system as the test student

`POST /teacher/subjects/{subject_id}/test-student/enter` SHALL:

1. Look up the test student for the subject.
2. Issue a JWT for the test student's user account using the standard `create_access_token` function.
3. Set the `access_token` cookie with the new JWT.
4. Redirect to `/portal`.

Only the subject owner SHALL be permitted. Non-owners receive HTTP 403. If no test student exists for the subject, HTTP 404 is returned.

#### Scenario: Teacher enters as test student
- **WHEN** the owner POSTs to `/teacher/subjects/5/test-student/enter`
- **THEN** the `access_token` cookie is replaced with a JWT for the test student's user and the browser is redirected to `/portal`

#### Scenario: Non-owner cannot enter as test student
- **WHEN** a teacher who is not the owner POSTs to `/teacher/subjects/5/test-student/enter`
- **THEN** HTTP 403 is returned and the cookie is unchanged

#### Scenario: Enter endpoint returns 404 when no test student exists
- **WHEN** the owner POSTs to `/teacher/subjects/5/test-student/enter` but no test student has been provisioned
- **THEN** HTTP 404 is returned

### Requirement: Test students are excluded from all statistics and counts

Every query that counts or lists enrolled students SHALL add a filter `Student.type = 'REAL'` (or equivalent join predicate).

Affected call sites (minimum):
- Dashboard enrolled_count subquery.
- Subject detail student list.
- Assignment statistics rows query.

Test students SHALL NOT appear in enrollment CSV exports.

#### Scenario: Enrolled count excludes test student
- **WHEN** a subject has 10 real students and 1 test student enrolled
- **THEN** the teacher dashboard shows `enrolled_count = 10` for that subject

#### Scenario: Assignment stats exclude test student
- **WHEN** the teacher views the assignment detail page for a subject with a test student
- **THEN** the test student does not appear in the per-student submission table

#### Scenario: Test student excluded from enrollment CSV
- **WHEN** the teacher downloads the enrollment CSV for a subject that has a test student
- **THEN** the test student row is not included in the CSV
