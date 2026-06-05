## ADDED Requirements

### Requirement: FEEDBACK_REQUEST notification case exists in the system
The system SHALL define a `FEEDBACK_REQUEST` value in the `NotificationCase` enum. This case SHALL appear on the student's notification preferences page alongside the existing `SUBMISSION_CHECKED` case, labelled "Feedback Request". The opt-out model applies: a missing preference row means the notification is enabled.

#### Scenario: Student sees FEEDBACK_REQUEST preference on preferences page
- **WHEN** the student navigates to `/portal/notification-preferences`
- **THEN** the page SHALL display a row for "Feedback Request" with an email toggle, defaulting to enabled if no preference row exists

#### Scenario: Student disables FEEDBACK_REQUEST email notifications
- **WHEN** the student toggles off the "Feedback Request" email preference
- **THEN** a `NotificationPreference` record with `case=FEEDBACK_REQUEST`, `method=EMAIL`, `enabled=False` is persisted and subsequent feedback request emails are suppressed for that student
