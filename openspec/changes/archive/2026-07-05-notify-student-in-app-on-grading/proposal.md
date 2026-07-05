## Why

A student currently learns their submission's outcome only by manually revisiting the assignment
page — there is no push notification of any kind for the most common path. Specifically:

- `execute_check_task`'s `test_failed` transition (checks ran, student didn't pass) and the
  `tests_only` review-mode's terminal `test_passed_tests_only` transition — together the outcome
  for every assignment with no teacher/AI review step, i.e. the majority case — enqueue **no
  notification at all**, email or in-app.
- `execute_submission_reviewed_task` (teacher approve/reject) already sends an email, but the
  app already has a generic in-app notification system (`Notification` model, bell icon,
  `/notifications` feed, `push_notification()` helper) that this path never uses.

A student has to know to go check, rather than being told.

## What Changes

- `execute_check_task` pushes an in-app notification when a submission's checks complete
  directly to a terminal state with no further review step: `test_failed` (didn't pass) and
  `test_passed_tests_only` (passed, `tests_only` review mode) — the two cases where the student's
  grade is now the final word and nothing else will notify them.
- `execute_submission_reviewed_task` pushes an in-app notification alongside its existing email
  when a teacher approves or rejects a submission.
- Both use the existing `push_notification(db, user_id, title, body, link)` helper — no new
  notification mechanism, just wiring the existing one into two paths that never called it.

## Capabilities

### New Capabilities
- `student-notifications`: documents when the student-facing in-app notification feed receives
  entries — previously the `Notification` model/feed/bell existed with no spec describing what
  triggers an entry.

## Impact

- `src/submissions_checker/workers/tasks/check_tasks.py` — push a notification at the
  `test_failed` and `test_passed_tests_only` transition points.
- `src/submissions_checker/workers/tasks/notification_tasks.py` — `execute_submission_reviewed_task`
  pushes an in-app notification in addition to its existing email.
- No schema change: `push_notification()` and the `notifications` table already exist.
- Out of scope: `tests_then_ai`, `tests_then_teacher`, `tests_then_ai_then_teacher`, and
  `tests_then_quiz` review modes — those already have (or will separately resolve to) their own
  notification at whatever step actually finalizes the outcome (teacher review, AI review, quiz
  result); adding in-app notifications to every intermediate step is a larger, separate scope.
