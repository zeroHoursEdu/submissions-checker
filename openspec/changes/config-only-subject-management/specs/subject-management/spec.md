## ADDED Requirements

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

## MODIFIED Requirements

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
