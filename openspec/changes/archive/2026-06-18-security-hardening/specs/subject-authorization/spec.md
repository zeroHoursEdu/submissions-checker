# subject-authorization

## ADDED Requirements

### Requirement: Subject-scoped object-level authorization
A teacher SHALL only access subject-scoped resources for subjects they own;
ADMIN users may access all. Non-owners receive 403.

#### Scenario: Teacher reads another teacher's subject
- **WHEN** a teacher requests a subject, roster, grades, submissions, or
  feedback for a subject they do not own
- **THEN** the response is 403

#### Scenario: Teacher mutates another teacher's subject
- **WHEN** a teacher enrolls/unenrolls a student, reviews a submission, imports
  students, or requests feedback for a subject they do not own
- **THEN** the response is 403 and no state changes

#### Scenario: Owner access permitted
- **WHEN** the owning teacher (or an ADMIN) accesses the subject
- **THEN** the request succeeds

### Requirement: Analytics scoped to owned subjects
Teacher analytics SHALL be limited to subjects owned by the requesting teacher;
ADMIN may view all.

#### Scenario: Cross-tenant student analytics blocked
- **WHEN** a teacher requests analytics for a student not enrolled in any of
  their subjects
- **THEN** the response is 403 (or excludes that student's data)

### Requirement: Safe redirect on language switch
The language switcher SHALL only redirect to same-origin relative paths.

#### Scenario: External redirect blocked
- **WHEN** `set_language` is called with an absolute/external `Referer`
- **THEN** the redirect falls back to a safe local path
