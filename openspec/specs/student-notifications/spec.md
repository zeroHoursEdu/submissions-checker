# student-notifications

## Purpose

Defines when the student-facing in-app notification feed (bell icon, `/notifications`) receives
an entry — distinct from email, which is governed separately by `notification-preferences`.

## Requirements

### Requirement: Student is notified in-app when checks resolve with no further review
When a submission's automated checks resolve directly to a final outcome with no further review
step (fails the checks, or passes under `tests_only` review mode), the system SHALL push an
in-app notification to the student via the existing notification feed, including the assignment
title, the outcome, and a link back to the assignment page.

#### Scenario: Failed checks notify the student
- **WHEN** a submission's automated checks complete and the submission does not pass
- **THEN** an in-app notification is created for the student naming the assignment and that it
  did not pass, linking to the assignment page

#### Scenario: Tests-only pass notifies the student
- **WHEN** a submission passes under `tests_only` review mode (no teacher/AI review configured)
- **THEN** an in-app notification is created for the student naming the assignment and that it
  passed, linking to the assignment page

#### Scenario: Intermediate review steps do not double-notify
- **WHEN** a submission passes under `tests_then_teacher`, `tests_then_ai`,
  `tests_then_ai_then_teacher`, or `tests_then_quiz` review mode
- **THEN** no in-app notification is created at this step — the outcome is not yet final

### Requirement: Student is notified in-app when a teacher reviews their submission
When a teacher approves or rejects a submission, the system SHALL push an in-app notification to
the student, in addition to the existing email notification.

#### Scenario: Approval notifies the student in-app
- **WHEN** a teacher approves a submission under review
- **THEN** an in-app notification is created for the student, alongside the existing email

#### Scenario: Rejection notifies the student in-app
- **WHEN** a teacher rejects a submission under review with a reason
- **THEN** an in-app notification is created for the student including the reason, alongside the
  existing email

### Requirement: In-app grading notifications are not gated by email preferences
The in-app notification for a graded/reviewed submission SHALL be created regardless of the
student's `SUBMISSION_CHECKED / EMAIL` preference — that preference only suppresses email.

#### Scenario: Email disabled, in-app notification still created
- **WHEN** a student has disabled the `SUBMISSION_CHECKED / EMAIL` preference and their teacher
  reviews a submission
- **THEN** no email is sent, but an in-app notification is still created
