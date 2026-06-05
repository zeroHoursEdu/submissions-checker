## MODIFIED Requirements

### Requirement: Teacher dashboard shows only ACTIVE subjects

The teacher dashboard query SHALL filter `subjects` to `status = ACTIVE`.

The enrolled count displayed on the dashboard tile SHALL exclude students where `students.type = 'TEST'`.

#### Scenario: Deleted subjects hidden from dashboard
- **WHEN** a subject has `status = DELETED`
- **THEN** it does not appear in the teacher dashboard subject list

#### Scenario: Enrolled count excludes test students
- **WHEN** a subject has 10 real students and 1 test student
- **THEN** the teacher dashboard tile shows `enrolled_count = 10`
