## 1. Database Models & Migrations

- [x] 1.1 Add `FEEDBACK_REQUEST_SENT` to `OutboxEventType` enum and `FEEDBACK_REQUEST` to `NotificationCase` enum in `enums.py`
- [x] 1.2 Create `Semester` SQLAlchemy model (`semesters` table: id BIGINT PK, name VARCHAR(50), season VARCHAR(10) SPRING|FALL, start_date DATE, end_date DATE; index on start_date, end_date)
- [x] 1.3 Write Alembic migration `0016_add_semesters.py`: create `semesters` table and seed 20 rows — Spring 2026 (Feb 1–Jun 30) through Fall 2035 (Sep 1–Jan 31, 2036), two semesters per calendar year
- [x] 1.4 Create `FeedbackRequest` SQLAlchemy model (`feedback_requests` table: id, subject_id FK, semester_id FK → semesters.id, created_at, created_by_teacher_id; unique constraint on `(subject_id, semester_id)`)
- [x] 1.5 Create `FeedbackToken` SQLAlchemy model (`feedback_tokens` table: id, feedback_request_id FK, student_id FK, token UNIQUE, used_at nullable)
- [x] 1.6 Create `FeedbackResponse` SQLAlchemy model (`feedback_responses` table: id, feedback_token_id UNIQUE FK, subject_id FK, rating INT 1-5, went_well TEXT, went_bad TEXT, to_change TEXT, submitted_at)
- [x] 1.7 Export new models from `db/models/__init__.py`
- [x] 1.8 Write Alembic migration `0017_add_feedback_tables.py` creating `feedback_requests`, `feedback_tokens`, `feedback_responses` with all constraints and indexes

## 2. Notification Infrastructure

- [x] 2.1 Add `FEEDBACK_REQUEST` case to `_ALL_CASES` list in `student_portal.py` notification preferences route (label: "Feedback Request")
- [x] 2.2 Add `execute_feedback_request_task` function in `notification_tasks.py` that: loads student + subject, checks `FEEDBACK_REQUEST` email preference, renders and sends email with the token link
- [x] 2.3 Wire the new task into the outbox worker dispatch table (wherever `SUBMISSION_REVIEWED` is dispatched)
- [x] 2.4 Create email template `feedback_request_email.html` with subject name and token link

## 3. Teacher — Feedback Request Route

- [x] 3.1 Add `POST /teacher/subjects/{subject_id}/feedback/request` route in `teacher_portal.py`: query current semester (`SELECT * FROM semesters WHERE start_date <= today <= end_date`), 404 if no active semester (summer gap), check for existing `FeedbackRequest` for `(subject_id, semester_id)`, create record, fan-out one `OutboxMessage` per enrolled student, handle `IntegrityError` for double-submit
- [x] 3.2 Add `GET /teacher/subjects/{subject_id}/feedback` route in `teacher_portal.py`: load all `FeedbackResponse` records with joined student name/email, compute average rating, render teacher feedback view template
- [x] 3.3 Add `GET /teacher/subjects/{subject_id}/feedback/export.csv` route in `teacher_portal.py`: stream CSV with columns student_name, student_email, rating, went_well, went_bad, to_change, submitted_at; filename `feedback_<subject_id>.csv`
- [x] 3.4 Update `teacher_subject.html` template: add "Request Feedback" button (enabled/disabled based on current-semester `FeedbackRequest`) and a link to the feedback view page

## 4. Teacher — Feedback View Template

- [x] 4.1 Create `templates/teacher_feedback_view.html`: summary header with average rating, response count, and "Export CSV" button; list of response cards (student name + email, rating stars, three answer blocks); empty/no-campaign states

## 5. Student — Tokenised Feedback Form Routes

- [x] 5.1 Create new router file `src/submissions_checker/api/routes/feedback.py` (no auth middleware) with:
  - `GET /feedback/{token}` — validate token, render form or already-submitted/not-found pages
  - `POST /feedback/{token}` — validate token unused, validate rating 1-5, insert `FeedbackResponse`, mark token `used_at`, redirect to thank-you
- [x] 5.2 Register the new feedback router in the main FastAPI app

## 6. Student — Feedback Form Templates

- [x] 6.1 Create `templates/feedback_form.html`: standalone page (no nav/auth chrome), subject name heading, star rating widget (1-5), three textarea fields with hardcoded question labels, submit button
- [x] 6.2 Create `templates/feedback_already_submitted.html`: informational page shown when token is already used
- [x] 6.3 Create `templates/feedback_thank_you.html`: confirmation page shown after successful submission
