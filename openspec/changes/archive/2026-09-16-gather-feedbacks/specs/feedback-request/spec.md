## ADDED Requirements

### Requirement: Teacher can request feedback for a subject once per semester
The system SHALL allow a teacher to trigger a feedback collection campaign for a subject exactly once per active semester. The current semester is resolved by looking up the `semesters` table row whose `start_date ≤ today ≤ end_date`. Spring semesters run Feb 1–Jun 30; Fall semesters run Sep 1–Jan 31. During the summer gap (Jul–Aug) no semester is active and the button SHALL be hidden with an explanatory note. A second POST within the same semester SHALL be rejected without sending duplicate notifications.

#### Scenario: Teacher triggers feedback for the current semester for the first time
- **WHEN** the teacher clicks "Request Feedback" on a subject page during an active semester and no `FeedbackRequest` exists for `(subject_id, current semester_id)`
- **THEN** the system inserts a `FeedbackRequest` record linking to the resolved `Semester` row, enqueues one outbox message per enrolled student with event type `FEEDBACK_REQUEST_SENT`, and redirects back to the subject page showing a success banner

#### Scenario: Teacher attempts to request feedback a second time in the same semester
- **WHEN** a `FeedbackRequest` already exists for the subject and the current semester and the teacher submits the form again (e.g. double-click or direct POST)
- **THEN** the system returns the subject page with an informational message indicating the request was already sent for this semester, without inserting duplicate records or sending any emails

#### Scenario: Teacher requests feedback in a new semester
- **WHEN** the teacher clicks "Request Feedback" on a subject that had a `FeedbackRequest` in a prior semester but none in the current semester
- **THEN** the system inserts a new `FeedbackRequest` for the current semester and fans out emails to all enrolled students

#### Scenario: Subject has no enrolled students
- **WHEN** the teacher triggers feedback for a subject with zero enrolled students
- **THEN** the system inserts the `FeedbackRequest` record but enqueues no outbox messages, and shows the subject page with a warning that no students are enrolled

### Requirement: Feedback request button state reflects current-semester campaign status
The teacher's subject detail page SHALL reflect the current semester's feedback campaign state. During the summer gap the button is replaced by a note. Otherwise: enabled when no request exists for the current semester, disabled/replaced when one has been sent.

#### Scenario: Subject page during summer gap (no active semester)
- **WHEN** the teacher views a subject page and `today` falls between Jul 1 and Aug 31
- **THEN** the page SHALL display an informational note such as "Feedback requests are only available during an active semester" with no active button

#### Scenario: Subject page before any feedback request this semester
- **WHEN** the teacher views a subject page during an active semester and there is no `FeedbackRequest` for `(subject_id, current semester_id)`
- **THEN** the page SHALL display an enabled "Request Feedback" button (even if a request was sent in a prior semester)

#### Scenario: Subject page after feedback request sent this semester
- **WHEN** the teacher views a subject page that already has a `FeedbackRequest` for the current semester
- **THEN** the page SHALL display a disabled state (e.g. greyed-out button or status chip) instead of the active button
