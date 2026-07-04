## MODIFIED Requirements

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

#### Scenario: Admin can delete another teacher's subject
- **WHEN** an ADMIN requests deletion of a subject owned by a different teacher
- **THEN** the request succeeds (not 403), consistent with ADMIN's access to every other
  subject-scoped route

#### Scenario: Admin can provision and enter a test student for another teacher's subject
- **WHEN** an ADMIN provisions a test student, or enters the app as the test student, for a
  subject owned by a different teacher
- **THEN** the request succeeds (not 403)

## ADDED Requirements

### Requirement: Teacher student roster is scoped to owned subjects
The teacher-facing student roster listing SHALL be scoped to students enrolled in subjects the
requesting teacher owns; an ADMIN SHALL see all students. A teacher who owns zero subjects SHALL
see zero students.

#### Scenario: Teacher sees only their own subjects' students
- **WHEN** a teacher who owns subject A (but not subject B) requests the student roster
- **THEN** only students enrolled in subject A appear in the response, not students enrolled
  only in subject B

#### Scenario: Teacher with no subjects sees an empty roster
- **WHEN** a teacher who owns no subjects requests the student roster
- **THEN** the response contains no students

#### Scenario: Admin sees the full roster
- **WHEN** an ADMIN requests the student roster
- **THEN** all students platform-wide appear in the response
