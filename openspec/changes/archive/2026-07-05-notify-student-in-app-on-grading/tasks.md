## 1. Auto-graded completion (check_tasks.py)

- [x] 1.1 In `src/submissions_checker/workers/tasks/check_tasks.py`, add a small helper `async def _notify_student(db, student_id, title, body, link) -> None` that resolves `user_id` via `select(User.id).where(User.student_id == student_id)` and calls `push_notification(db, user_id, title, body, link)`, no-op if no matching user row.
- [x] 1.2 At the `test_failed` transition (~line 150-152), call the helper with a title/body naming the assignment and that it did not pass, linking to `/portal/subjects/{subject_id}/assignments/{sa_id}`.
- [x] 1.3 In `_advance_after_tests`'s `tests_only` branch (the final `else` before `test_passed_tests_only`), call the helper with a title/body naming the assignment and that it passed, same link pattern.

## 2. Teacher review (notification_tasks.py)

- [x] 2.1 In `execute_submission_reviewed_task`, after the existing email-suppression check (or independent of it — the in-app push must NOT be behind that check), call `push_notification` with a title/body reflecting approve/reject (+ reason on reject) and the existing `portal_url` as the link. Ensure this call happens regardless of whether the email was suppressed by preference.

## 3. Tests

- [x] 3.1 Add/extend a unit or functional test asserting a `test_failed` transition creates a `Notification` row for the submitting student's user account.
- [x] 3.2 Add a test asserting a `tests_only` passing completion creates a `Notification` row.
- [x] 3.3 Add a test asserting `tests_then_teacher`/other multi-step review modes do NOT create a notification at the initial test-passing step.
- [x] 3.4 Add/extend a test asserting `execute_submission_reviewed_task` creates a `Notification` row on both approve and reject, and that it still creates one even when the student has disabled the `SUBMISSION_CHECKED / EMAIL` preference (email suppressed, in-app not).
- [x] 3.5 Run the full unit + functional test suite to confirm no regression.
