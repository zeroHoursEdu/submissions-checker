## MODIFIED Requirements

### Requirement: Submission reviewed notification respects student preferences
The system SHALL check the student's `SUBMISSION_CHECKED / EMAIL` preference before dispatching a submission-reviewed email. If the student has disabled the preference, or if the system preference defaults to ON and no row exists, the behavior is as follows:

- No preference row → email IS sent (default ON / opt-out model)
- Preference row with `enabled = True` → email IS sent
- Preference row with `enabled = False` → email is NOT sent

#### Scenario: Student has not configured preferences (default)
- **WHEN** `execute_submission_reviewed_task` fires for a student with no preference row
- **THEN** the email is dispatched as before

#### Scenario: Student has email enabled
- **WHEN** `execute_submission_reviewed_task` fires and the student's `SUBMISSION_CHECKED / EMAIL` preference is `enabled = True`
- **THEN** the email is dispatched

#### Scenario: Student has email disabled
- **WHEN** `execute_submission_reviewed_task` fires and the student's `SUBMISSION_CHECKED / EMAIL` preference is `enabled = False`
- **THEN** no email is sent and the task logs `submission_reviewed_email_suppressed` at INFO level
