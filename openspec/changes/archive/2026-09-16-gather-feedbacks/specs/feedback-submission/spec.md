## ADDED Requirements

### Requirement: Student receives a personalised feedback link
The system SHALL email each enrolled student a URL containing a unique opaque token (`/feedback/<token>`) when a feedback request is triggered. The token SHALL be a cryptographically random string of at least 32 URL-safe bytes. Delivery SHALL respect the student's `FEEDBACK_REQUEST` notification preference; if the student has opted out, no email is sent and the token record is still created (preserving the opt-out audit trail without breaking the campaign).

#### Scenario: Email delivered to student with default preferences
- **WHEN** the outbox worker processes a `FEEDBACK_REQUEST_SENT` event for a student who has not opted out of `FEEDBACK_REQUEST` email notifications
- **THEN** the worker sends an email to the student's address containing a unique link in the form `/feedback/<token>`

#### Scenario: Email suppressed for student who opted out
- **WHEN** the outbox worker processes a `FEEDBACK_REQUEST_SENT` event for a student whose `FEEDBACK_REQUEST` / `EMAIL` preference is disabled
- **THEN** the worker skips sending the email and logs the suppression; the token record remains in the database

### Requirement: Feedback form is accessible via token
The system SHALL render a feedback form at `GET /feedback/<token>` without requiring the student to be logged in. The form SHALL display the subject name and the following hardcoded questions:
1. "What went well this semester?"
2. "What didn't go well?"
3. "What would you change for next semester?"
4. An overall rating from 1 to 5 stars

#### Scenario: Student opens a valid unused token link
- **WHEN** a GET request is made to `/feedback/<token>` and the token exists and has not been used
- **THEN** the system renders the feedback form with the subject name and all four input fields

#### Scenario: Student opens an invalid or expired token
- **WHEN** a GET request is made to `/feedback/<token>` and the token does not exist in the database
- **THEN** the system renders a 404 error page

#### Scenario: Student opens an already-used token
- **WHEN** a GET request is made to `/feedback/<token>` and the token's `used_at` is not null
- **THEN** the system renders an informational page stating the feedback has already been submitted

### Requirement: Student submits feedback exactly once per token
The system SHALL accept a POST to `/feedback/<token>` with the rating and three text answers, record the response, mark the token as used, and show a thank-you page. A second POST with the same token SHALL be rejected.

#### Scenario: Successful first submission
- **WHEN** the student submits the form with a rating (1–5) and answers to all three questions
- **THEN** the system inserts a `FeedbackResponse` record linked to the token and subject, sets `feedback_tokens.used_at` to the current timestamp, and renders a thank-you page

#### Scenario: Rating out of range
- **WHEN** the student submits with a rating outside the 1–5 range (e.g. via form manipulation)
- **THEN** the system rejects the submission with a 422 error and does not create a response record

#### Scenario: Re-submission on used token
- **WHEN** a POST is made to `/feedback/<token>` and the token is already marked as used
- **THEN** the system returns the already-submitted page without creating a duplicate response record
