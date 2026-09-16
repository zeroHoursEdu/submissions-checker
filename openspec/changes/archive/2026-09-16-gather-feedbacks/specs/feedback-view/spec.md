## ADDED Requirements

### Requirement: Teacher can view all feedback responses for a subject
The system SHALL provide a read-only page at `GET /teacher/subjects/{subject_id}/feedback` accessible only to the authenticated teacher. The page SHALL list all submitted `FeedbackResponse` records for that subject. If no feedback request has been sent yet, the page SHALL display an appropriate empty state.

#### Scenario: Teacher views feedback after responses are collected
- **WHEN** the teacher navigates to the feedback view for a subject with at least one submitted response
- **THEN** the page displays each response as a card showing: the student's full name and email, the star rating, and the three text answers (one per question)

#### Scenario: Teacher views feedback before any responses are submitted
- **WHEN** the teacher navigates to the feedback view for a subject where a feedback request was sent but no student has responded yet
- **THEN** the page displays an empty state message indicating no responses have been collected yet

#### Scenario: Teacher views feedback before request was sent
- **WHEN** the teacher navigates to the feedback view for a subject with no feedback request
- **THEN** the page displays a state indicating the feedback campaign has not been started

#### Scenario: Unauthorized access attempt
- **WHEN** a non-teacher user (or unauthenticated request) accesses the feedback view URL
- **THEN** the system returns a 403 or redirects to login

### Requirement: Feedback view presents aggregate summary
The page SHALL display the average star rating across all responses at the top of the list.

#### Scenario: Average rating calculation
- **WHEN** the feedback view is rendered with multiple responses
- **THEN** the page SHALL show the arithmetic mean of all ratings rounded to one decimal place alongside the total response count

### Requirement: Teacher can export feedback responses to CSV
The system SHALL provide a download endpoint at `GET /teacher/subjects/{subject_id}/feedback/export.csv` accessible only to the authenticated teacher. The CSV SHALL include one row per submitted response with columns: `student_name`, `student_email`, `rating`, `went_well`, `went_bad`, `to_change`, `submitted_at`.

#### Scenario: Teacher downloads CSV with responses
- **WHEN** the teacher clicks "Export CSV" on the feedback view page for a subject with at least one response
- **THEN** the browser downloads a `text/csv` file named `feedback_<subject_id>.csv` containing all submitted responses, with a header row

#### Scenario: Teacher downloads CSV with no responses
- **WHEN** the teacher clicks "Export CSV" on the feedback view page for a subject with no responses
- **THEN** the browser downloads a CSV file containing only the header row
