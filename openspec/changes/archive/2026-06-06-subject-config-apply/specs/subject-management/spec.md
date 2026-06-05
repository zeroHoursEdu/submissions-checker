## ADDED Requirements

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

A teacher who is the `owner_id` of a subject SHALL be able to soft-delete it via `POST /teacher/subjects/{subject_id}/delete`.

Soft-delete SHALL set `status = DELETED`; the row is NOT removed from the database.

A "Remove Subject" button SHALL appear on the subject detail page only when `current_user.id == subject.owner_id`.

#### Scenario: Owner soft-deletes subject
- **WHEN** the owner clicks "Remove Subject" and confirms
- **THEN** `subjects.status` is set to `DELETED` and the subject no longer appears on the teacher dashboard

#### Scenario: Non-owner cannot delete
- **WHEN** a teacher who is not the owner POSTs to the delete endpoint
- **THEN** the system returns HTTP 403 and the subject status remains `ACTIVE`

### Requirement: Teacher dashboard shows only ACTIVE subjects

The teacher dashboard query SHALL filter `subjects` to `status = ACTIVE`.

#### Scenario: Deleted subjects hidden from dashboard
- **WHEN** a subject has `status = DELETED`
- **THEN** it does not appear in the teacher dashboard subject list

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
