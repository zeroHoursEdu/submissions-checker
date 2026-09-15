## ADDED Requirements

### Requirement: Teacher enrols students into a subject from a CSV

The teacher subject page SHALL present an enrolment panel that accepts a CSV upload and POSTs it
to `POST /teacher/subjects/{subject_id}/students/import`.

The CSV SHALL have a header row with exactly two recognised columns: `email` and `variant`.
`email` SHALL be required. `variant` SHALL be permitted to be empty for any row, and MAY be
omitted entirely as a column for subjects that declare no variants.

The endpoint SHALL require that the caller has access to the subject, using the same ownership
check applied by the other subject routes.

A file larger than 1 MB SHALL be rejected with HTTP 413, and a file that is not valid UTF-8 SHALL
be rejected with HTTP 422, matching the existing import endpoints.

A CSV whose header lacks an `email` column SHALL be rejected with HTTP 422 naming the missing
column, and no row SHALL be processed.

#### Scenario: Enrolment panel is reachable
- **WHEN** a teacher views a subject page
- **THEN** an enrolment panel with a file picker and a submit button is present, and it posts to
  the subject's student-import endpoint

#### Scenario: Header missing the email column
- **WHEN** a CSV whose header is `mail,variant` is uploaded
- **THEN** the request fails with HTTP 422 naming `email` as missing, and no student is enrolled

### Requirement: Enrolment matches existing students by e-mail and never creates them

Each row's `email` SHALL be trimmed and lower-cased and then matched against `students.email`.

A row whose e-mail matches no existing student SHALL be rejected. The endpoint SHALL NOT create
a student, a user account, a group, or a `SEND_CREDENTIALS` outbox message. Creating students
and sending invitations remains the responsibility of
`POST /teacher/students/import`.

A rejected row SHALL NOT prevent the remaining rows from being processed.

#### Scenario: Unknown e-mail is rejected, known ones still enrol
- **WHEN** a CSV contains one address belonging to a registered student and one belonging to
  nobody
- **THEN** the registered student is enrolled, no student is created for the unknown address,
  and the result reports one enrolment and one rejection

#### Scenario: No invitation is sent
- **WHEN** a student is enrolled through this endpoint
- **THEN** no `SEND_CREDENTIALS` outbox message is written and the student receives no e-mail

### Requirement: Enrolment fans out assignment rows and applies the variant

For each accepted row the endpoint SHALL ensure a `subjects_students` row exists linking the
student to the subject, creating it when absent and leaving an existing one untouched.

The endpoint SHALL then ensure a `students_assignments` row exists for that student against
**every** `subjects_assignments` row of the subject, matching the fan-out already performed when
a single student is enrolled.

When the row supplies a non-empty `variant`, that value SHALL be written to the `variant` column
of every one of those `students_assignments` rows, replacing any previous value. When the
`variant` cell is empty or the column is absent, existing variant values SHALL be left unchanged.

Re-uploading the same file SHALL be safe: no duplicate `subjects_students` or
`students_assignments` rows SHALL be created.

#### Scenario: New enrolment creates every assignment row
- **WHEN** a student who is not yet enrolled is accepted from the CSV and the subject has seven
  assignments
- **THEN** one `subjects_students` row and seven `students_assignments` rows exist for that
  student

#### Scenario: Variant is written to every assignment of the subject
- **WHEN** a row supplies `variant` of `3`
- **THEN** every `students_assignments` row for that student in that subject has `variant = "3"`

#### Scenario: Empty variant preserves the existing value
- **WHEN** a student already has `variant = "5"` and is re-uploaded with an empty `variant` cell
- **THEN** the stored variant remains `"5"`

#### Scenario: Re-upload is idempotent
- **WHEN** the identical CSV is uploaded a second time
- **THEN** the row counts in `subjects_students` and `students_assignments` are unchanged

### Requirement: The result of an enrolment upload is reported back per row

After processing, the teacher SHALL be returned to the subject page with a summary stating how
many students were newly enrolled, how many were already enrolled, and how many rows were
rejected.

Each rejected row SHALL be reported with its 1-based line number in the CSV — counting the header
as line 1 — and the reason for rejection, which SHALL distinguish an unknown e-mail from an empty
e-mail cell.

#### Scenario: Summary counts are shown
- **WHEN** a CSV of ten rows enrols six new students, finds three already enrolled and rejects
  one
- **THEN** the subject page reports 6 enrolled, 3 already enrolled and 1 rejected

#### Scenario: Rejected row names its line and reason
- **WHEN** the address on CSV line 4 belongs to no registered student
- **THEN** the report identifies line 4 and states that the e-mail is not a registered student

### Requirement: The example CSV is generated from the subject's own config

The subject page SHALL offer a download of an example CSV from
`GET /teacher/subjects/{subject_id}/students/template.csv`.

The file SHALL have the header `email,variant` and SHALL contain one row for each distinct
variant declared anywhere in the subject's active `SubjectPluginConfig`, read from the
`variants` mapping of each assignment. Variant identifiers SHALL be the real keys from the
config, not invented values, so that a teacher can copy them directly.

Each row SHALL carry a placeholder e-mail address that is visibly an example and cannot collide
with a real student.

When the subject declares no variants at all, the file SHALL contain the header and two
placeholder rows with an empty `variant` cell, so the expected shape is still obvious.

#### Scenario: Variants come from the config
- **WHEN** a subject's config declares variants `1`, `2` and `3` across its assignments
- **THEN** the downloaded CSV has three rows carrying exactly `1`, `2` and `3` in the variant
  column

#### Scenario: Variants declared on different assignments are merged
- **WHEN** one assignment declares variants `1` and `2` and another declares `2` and `5`
- **THEN** the downloaded CSV has one row each for `1`, `2` and `5`

#### Scenario: Subject without variants still gets a usable example
- **WHEN** a subject declares no variants on any assignment
- **THEN** the downloaded CSV has the `email,variant` header and two placeholder rows whose
  variant cell is empty

#### Scenario: Placeholder addresses are not real students
- **WHEN** the example CSV is downloaded
- **THEN** its e-mail values are recognisable placeholders and uploading the file unchanged
  enrols nobody
