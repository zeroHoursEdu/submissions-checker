## Why

Teachers currently have no way to collect structured feedback from students at the end of a semester. Gathering this data through the platform—where enrollment is already tracked—removes the need for external survey tools and ensures responses are tied to real enrolled students.

## What Changes

- New "Request Feedback" button on the teacher's subject page, submittable only once per semester per subject
- A `FeedbackRequest` record (unique on `subject_id` + `semester`) tracks whether a teacher has already sent the request for the current semester
- Students enrolled in the subject receive a notification (email by default, respecting their notification preferences) with a one-time personal link to submit feedback
- A dedicated feedback form with a hardcoded 5-star rating and three open-ended questions (what went well, what went badly, what to change next semester)
- Each student's link is single-use; submitting marks the token as consumed
- Teacher view shows all submitted feedbacks for a subject in a Google Forms–style read-only UI, including student names
- Teacher can export all responses for a subject to CSV

## Capabilities

### New Capabilities
- `feedback-request`: Teacher triggers a one-time-per-semester feedback campaign per subject; system fans out personalized links to all enrolled students
- `feedback-submission`: Student opens a tokenized link, fills the hardcoded form, submits once
- `feedback-view`: Teacher reads all collected feedback responses for a subject

### Modified Capabilities
- `student-notifications`: Add `FEEDBACK_REQUEST` notification case so students can opt out of feedback request emails via their existing notification preferences page

## Impact

- **New DB tables**: `semesters`, `feedback_requests`, `feedback_tokens`, `feedback_responses`
- **New routes**: teacher portal (POST trigger, GET view), student portal (GET form, POST submit)
- **New Alembic migration**
- **Outbox/notification**: new outbox event type `FEEDBACK_REQUEST_SENT` and a new celery task that fans out emails with tokenized links
- **Templates**: feedback form page, teacher feedback view page, student notification email template
- **Notification preferences**: extend `NotificationCase` enum with `FEEDBACK_REQUEST`
