## ADDED Requirements

### Requirement: Role-correct post-login redirect
Post-login redirect SHALL send each role to the area it can actually access: `TEACHER` and
`ADMIN` to their respective dashboards, all other roles to the student portal. An `ADMIN` SHALL
NOT be redirected to the student portal.

#### Scenario: Admin lands on the admin dashboard
- **WHEN** a user with role `ADMIN` logs in successfully
- **THEN** the redirect target is `/admin`, not `/portal`

#### Scenario: Teacher redirect unchanged
- **WHEN** a user with role `TEACHER` logs in successfully
- **THEN** the redirect target is `/teacher`

#### Scenario: Student redirect unchanged
- **WHEN** a user with a student-facing role logs in successfully
- **THEN** the redirect target is `/portal`
