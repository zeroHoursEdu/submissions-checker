## ADDED Requirements

### Requirement: Student can view notification preferences
The system SHALL provide a page at `GET /portal/notification-preferences` showing all available notification cases and methods, with the current enabled/disabled state for the authenticated student.

#### Scenario: Student visits preferences page
- **WHEN** an authenticated student navigates to `/portal/notification-preferences`
- **THEN** the page renders a list of notification cases (e.g., "Submission Checked") with a toggle per method (e.g., "Email")
- **THEN** each toggle reflects the persisted preference, defaulting to ON when no row exists

### Requirement: Student can toggle a notification preference
The system SHALL allow a student to enable or disable a specific (case, method) preference via `POST /portal/notification-preferences/{case}/{method}/toggle`.

#### Scenario: Student disables email for submission checked
- **WHEN** a student posts to `/portal/notification-preferences/SUBMISSION_CHECKED/EMAIL/toggle` with current state ON
- **THEN** the preference row is upserted with `enabled = False`
- **THEN** the response redirects back to the preferences page

#### Scenario: Student enables email for submission checked
- **WHEN** a student posts to `/portal/notification-preferences/SUBMISSION_CHECKED/EMAIL/toggle` with current state OFF
- **THEN** the preference row is upserted with `enabled = True`
- **THEN** the response redirects back to the preferences page

#### Scenario: No preference row exists on first toggle
- **WHEN** no `notification_preferences` row exists for the student and case/method
- **THEN** the system creates a new row with `enabled` set to the opposite of the default (i.e., OFF on first disable, ON on first enable)

### Requirement: Preferences are student-scoped
The system SHALL enforce that a student can only read and modify their own notification preferences.

#### Scenario: Accessing another student's preferences
- **WHEN** a student attempts to access or modify preferences for a different student_id
- **THEN** the system returns HTTP 403 or ignores the attempt (preferences are derived from the session user, not a URL parameter)
