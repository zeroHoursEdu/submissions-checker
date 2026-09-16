# Spec: Subject Management

## Purpose

Defines subject ownership, lifecycle (soft-delete), dashboard visibility rules, and the UI entry point for ZIP-based config upload.
## Requirements
### Requirement: Subject has an owner and a lifecycle status

The `subjects` table SHALL have an `owner_id` column (nullable BIGINT FK → `users.id`) representing the teacher who created the subject via ZIP upload.

The `subjects` table SHALL have a `status` column (`VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'`) with allowed values `ACTIVE` and `DELETED`.

A partial unique index SHALL enforce that at most one `ACTIVE` subject exists per `code`. Multiple `DELETED` rows with the same `code` SHALL be permitted.

#### Scenario: Unique active code enforced
- **WHEN** a new subject is created with `code = "CS101"` while another `ACTIVE` subject with `code = "CS101"` exists
- **THEN** the database constraint prevents the insert and the system returns HTTP 409

#### Scenario: Multiple deleted subjects allowed for same code
- **WHEN** a subject with `code = "CS101"` has `status = DELETED` and a new active subject with `code = "CS101"` is created
- **THEN** both rows coexist in the database without constraint violation

### Requirement: Owner can soft-delete their subject

A teacher who is the `owner_id` of a subject SHALL be able to soft-delete it via
`POST /teacher/subjects/{subject_id}/delete`.

Soft-delete SHALL set `status = DELETED`; the row is NOT removed from the database.

The endpoint SHALL remain available to API callers and SHALL continue to enforce ownership, but
the teacher subject page SHALL NOT present a "Remove Subject" button, because deleting a subject
is a destructive action that belongs with the same deliberate, reviewed process as creating it.

#### Scenario: Owner soft-deletes subject via the endpoint
- **WHEN** the owner POSTs to `/teacher/subjects/{subject_id}/delete`
- **THEN** `subjects.status` is set to `DELETED` and the subject no longer appears on the
  teacher dashboard

#### Scenario: Non-owner cannot delete
- **WHEN** a teacher who is not the owner POSTs to the delete endpoint
- **THEN** the system returns HTTP 403 and the subject status remains `ACTIVE`

#### Scenario: Subject page shows no delete button
- **WHEN** the owner views the subject detail page
- **THEN** no button or form targets the delete endpoint

### Requirement: Teacher dashboard shows only ACTIVE subjects

The teacher dashboard query SHALL filter `subjects` to `status = ACTIVE`.

The enrolled count displayed on the dashboard tile SHALL exclude students where `students.type = 'TEST'`.

#### Scenario: Deleted subjects hidden from dashboard
- **WHEN** a subject has `status = DELETED`
- **THEN** it does not appear in the teacher dashboard subject list

#### Scenario: Enrolled count excludes test students
- **WHEN** a subject has 10 real students and 1 test student
- **THEN** the teacher dashboard tile shows `enrolled_count = 10`

### Requirement: Teacher dashboard has an Upload Config button

The teacher dashboard SHALL display an "Upload Config" button that opens a file-picker restricted to `.zip` files and POSTs to `POST /teacher/subjects/apply-config`.

On success the page SHALL display a flash message indicating whether the subject was created or updated.

On error (403 ownership, 400 invalid ZIP, 500 server error) the page SHALL display the error message returned by the server.

#### Scenario: Config applied successfully — subject created
- **WHEN** a ZIP is uploaded and a new subject is created
- **THEN** the dashboard reloads with a success message containing the subject name and "created"

#### Scenario: Config applied successfully — subject updated
- **WHEN** a ZIP is uploaded and an existing subject is updated
- **THEN** the dashboard reloads with a success message containing the subject name and "updated"

### Requirement: No manual subject-creation form exists
The teacher dashboard SHALL NOT present a manual create-subject form or button. ZIP-based config
upload (`POST /teacher/subjects/apply-config`) SHALL be the only way to create or update a
subject.

#### Scenario: Dashboard has no dead create-subject link
- **WHEN** a teacher views the dashboard
- **THEN** the only subject-creation affordance is the Upload Config button; no link or form
  targets a separate manual-creation route

### Requirement: The teacher UI exposes no affordance for mutating subject or assignment content

A subject's content — its name, description, assignments, sandbox settings, grading weights and
quiz banks — SHALL be changed only by uploading a config ZIP through
`POST /teacher/subjects/apply-config`. This keeps the config file the single reviewed,
version-controlled source of truth and guarantees that what runs matches what is in the subject
repository.

The teacher subject page SHALL NOT present a link or form for editing the subject, deleting the
subject, creating an assignment, or exporting results.

The teacher assignment page SHALL NOT present a link or form for editing the assignment, editing
the quiz, or exporting results.

The teacher dashboard SHALL NOT present a link to the analytics page, which requires the ADMIN
role and returns HTTP 403 to an ordinary teacher.

The routes behind these affordances MAY continue to exist and to enforce their own
authorization; this requirement governs the UI only, so existing API clients and admin tooling
are unaffected.

Affordances that do NOT mutate subject content SHALL remain: Apply config, the test-student
panel, feedback collection, submission review, and student enrolment.

#### Scenario: Subject page offers no content-mutating action
- **WHEN** a teacher who owns a subject views its page
- **THEN** no element links to the subject edit form, the subject delete endpoint, the
  create-assignment form, or the CSV export
- **AND** the Apply config, test-student, feedback and enrolment affordances are still present

#### Scenario: Assignment page offers no content-mutating action
- **WHEN** a teacher views an assignment page
- **THEN** no element links to the assignment edit form, the quiz editor, or the CSV export
- **AND** the submission list and review links are still present

#### Scenario: Dashboard hides the admin-only analytics page
- **WHEN** a user with the TEACHER role views the dashboard
- **THEN** no element links to `/teacher/analytics`

#### Scenario: Config re-apply remains the update path
- **WHEN** a teacher needs to change an assignment's deadline, grading weights or quiz bank
- **THEN** the only affordance offered is uploading a new config ZIP, which creates a new
  `SubjectPluginConfig` version

