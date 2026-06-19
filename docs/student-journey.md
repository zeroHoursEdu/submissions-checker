# Student Journey

What a student can do in submissions-checker, from first login to final grade. Each
step lists the page/route behind it. Routes under the student portal use the `/portal`
prefix; auth uses `/auth`.

> Students authenticate with a `STUDENT`-role user account that is linked to a `Student`
> profile (created by a teacher via CSV import or single add). There is no public
> self-signup — see [student-registration.md](student-registration.md).

## 1. Log in

- `GET /auth/login` → `POST /auth/login` — username + password. A JWT is issued in an
  HTTP-only cookie (8-hour expiry).
- Forgot password: `GET/POST /auth/forgot-password` sends a reset link;
  `GET/POST /auth/reset-password?token=…` sets a new password (token is single-use,
  2-hour TTL).
- Each successful login is recorded (`UserLogin`) and feeds teacher fraud analytics.

## 2. Pick a language (optional)

- `POST /set-language` stores a language preference in a cookie (1-year expiry). UI
  strings come from the `i18n/` vocabularies (English + Ukrainian).

## 3. Give proctoring consent (one-time, if quizzes are used)

- `GET /portal/consent` → `POST /portal/consent` — the student acknowledges the
  recording/proctoring notice. Acceptance stamps `Student.recording_consent_at`.
- Consent is required **before** a proctored quiz can be opened.

## 4. Browse subjects and assignments

- `GET /portal` — grid of enrolled subjects with completion progress.
- `GET /portal/subjects/{subject_id}` — assignment list for a subject, with deadlines
  and per-assignment status.
- `GET /portal/subjects/{subject_id}/assignments/{sa_id}` — assignment detail: full
  description, attached content files (PDF/docs), submission history, current status,
  and the action buttons available for the current state.
- `GET /portal/summary` — cross-subject overview of the student's standing.

## 5. Submit an assignment (ZIP upload)

- `POST /portal/subjects/{subject_id}/assignments/{sa_id}/submit` — upload a ZIP
  (≤50 MB, validated for safety/path-traversal).
- Guards enforced at submit time: deadline / late policy and the per-assignment
  `max_submissions` limit. A similarity check compares the upload against prior
  submissions.
- A `Submission` record is created (`source_type=ZIP_UPLOAD`) and a `RUN_CHECKS`
  job is enqueued. The plugin config version is pinned to the submission so retries
  use the same checks.

## 6. Watch the submission get checked

The submission moves through a state machine driven by the assignment's `review_mode`:

1. **VALIDATING** → optional structural validation. Failure → `VALIDATION_FAILED`.
2. **TESTING** → the subject's automated checks run in an isolated Docker sandbox
   (no network, read-only, resource-limited). A score is computed from the test
   results and compared to `min_pass_score`.
3. Outcome depends on `review_mode`:
   - `tests_only` → **COMPLETED**.
   - `tests_then_ai` → AI review → COMPLETED.
   - `tests_then_teacher` → **AWAITING_TEACHER_REVIEW** (teacher grades).
   - `tests_then_ai_then_teacher` → AI review → teacher review.
   - `tests_then_quiz` → **QUIZ_SENT** (student must take a quiz, step 7).

The student sees per-test results on the assignment detail page (which test names and
descriptions are shown is controlled by the subject config).

## 7. Take a proctored quiz (when required)

- `GET /portal/subjects/{subject_id}/assignments/{sa_id}/quiz` — start an attempt
  (requires consent from step 3; respects `max_quiz_attempts`).
- `GET /portal/quiz/{attempt_id}` — the quiz page. Questions are snapshotted at start
  (optionally shuffled). Supported types: single-choice, multiple-choice, ordering,
  true/false, short-answer. An optional countdown enforces `time_limit_minutes`.
- `POST /portal/quiz/{attempt_id}/event` — the page reports anti-cheat events
  (tab switch, window blur, resize, copy attempt, keyboard shortcuts, right-click,
  fullscreen exit). Configured rules apply actions: warn, flag, reduce remaining time,
  or force-fail.
- `POST /portal/quiz/{attempt_id}/snapshot` — if webcam proctoring is enabled, frames
  are captured on violations and stored to S3.
- `POST /portal/quiz/{attempt_id}/submit` — finalize. Status becomes COMPLETED,
  TIMED_OUT, or VIOLATION_FAIL.
- `GET /portal/quiz/{attempt_id}/result` — graded score with per-question breakdown
  (correct answers shown only if the subject enables it).

## 8. See results and grades

- Final grades appear on the subject and assignment detail pages once a submission
  reaches COMPLETED (or after a teacher assigns a grade).
- In-app notifications: `GET /notifications`, mark read
  (`POST /notifications/{id}/read`, `POST /notifications/read-all`), unread badge via
  `GET /notifications/unread-count`.

## 9. Manage notification preferences

- `GET /portal/notification-preferences` — toggle email notifications per case
  (e.g. submission checked, feedback request) via
  `POST /portal/notification-preferences/{case}/{method}/toggle`.

## 10. Give course feedback

- When a teacher sends a feedback request, the student receives a tokenized link.
- `GET /feedback/{token}` → `POST /feedback/{token}` — a public (no-login) form: a
  1–5 rating plus free-text (what went well / what went badly / what to change).
  The token is single-use; `GET /feedback/{token}/thanks` confirms submission.
