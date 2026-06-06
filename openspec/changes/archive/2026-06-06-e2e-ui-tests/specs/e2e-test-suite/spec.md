## ADDED Requirements

### Requirement: Teacher authentication flow
The system SHALL allow a teacher (seeded in DB) to log in via the `/login` page and be redirected to the teacher dashboard.

#### Scenario: Successful teacher login
- **WHEN** a teacher with valid credentials visits `/login` and submits the form
- **THEN** the browser is redirected to `/teacher` and the dashboard is visible

#### Scenario: Failed teacher login with wrong password
- **WHEN** a teacher submits the login form with an incorrect password
- **THEN** the page shows an authentication error message and remains on `/login`

### Requirement: Subject creation via ZIP config upload
The system SHALL allow a teacher to upload a ZIP archive to create a new subject with predefined assignments.

#### Scenario: Teacher uploads valid ZIP config
- **WHEN** a teacher visits the teacher dashboard and uploads a valid subject config ZIP file
- **THEN** a new subject appears in the subjects list on the dashboard

#### Scenario: Teacher uploads invalid ZIP config
- **WHEN** a teacher uploads a malformed ZIP file
- **THEN** an error message is shown and no new subject is created

### Requirement: Student enrollment via CSV upload
The system SHALL allow a teacher to upload a CSV file to enroll students in a subject.

#### Scenario: Teacher uploads valid student CSV
- **WHEN** a teacher opens a subject page and uploads a CSV with student data
- **THEN** the enrolled student count increases and the student list reflects the new enrollments

#### Scenario: Student receives credentials after enrollment
- **WHEN** a student is enrolled via CSV upload
- **THEN** the student's generated username and temporary password are accessible from the enrollment response and can be used to log in

### Requirement: Student authentication flow
The system SHALL allow an enrolled student to log in with credentials generated during CSV enrollment.

#### Scenario: Student logs in with generated credentials
- **WHEN** an enrolled student visits `/login` and submits their generated credentials
- **THEN** the browser is redirected to the student portal at `/portal`

### Requirement: Student assignment submission cycle
The system SHALL support a student submitting work, receiving a FAIL result, resubmitting, and eventually receiving a PASS result.

#### Scenario: Student submits work and fails
- **WHEN** a student uploads a submission ZIP that does not meet assignment criteria
- **THEN** the submission status shows FAILED and appropriate feedback is displayed

#### Scenario: Student resubmits and passes
- **WHEN** a student uploads a corrected submission ZIP that meets all assignment criteria
- **THEN** the submission status shows PASSED

#### Scenario: Passed assignment cannot be resubmitted
- **WHEN** a student attempts to upload another submission for an already-passed assignment
- **THEN** the system blocks the upload and shows a "already passed" message

### Requirement: Analytics dashboard renders correctly
The system SHALL display an analytics page with submission statistics for teachers.

#### Scenario: Teacher views analytics dashboard
- **WHEN** a teacher navigates to the analytics page
- **THEN** the page renders without errors and shows submission count statistics for at least one subject

### Requirement: Student notification preferences
The system SHALL allow a student to configure notification channel preferences.

#### Scenario: Student disables email notifications
- **WHEN** a student visits the notification settings page and unchecks the email channel
- **THEN** the preference is saved and the page reloads showing email as disabled

#### Scenario: Student re-enables email notifications
- **WHEN** a student re-checks the email channel on the notification settings page
- **THEN** the preference is saved and the page reloads showing email as enabled

### Requirement: Feedback request and response flow
The system SHALL allow a teacher to request feedback on a submission and a student to respond.

#### Scenario: Teacher requests feedback on a submission
- **WHEN** a teacher opens a submission detail page and clicks "Request Feedback"
- **THEN** a feedback request is created and its status appears on the submission page

#### Scenario: Student responds to feedback request
- **WHEN** a student receives a feedback token link and submits a response
- **THEN** the response is saved and visible on the teacher's feedback view

### Requirement: Quiz flow for student
The system SHALL allow a student to attempt a quiz associated with an assignment.

#### Scenario: Student starts and completes a quiz
- **WHEN** a student navigates to a quiz-enabled assignment and submits answers
- **THEN** the quiz attempt result (pass or fail) is displayed and recorded

#### Scenario: Failed quiz attempt allows retry
- **WHEN** a student fails a quiz attempt
- **THEN** the student can start another attempt on the same quiz
