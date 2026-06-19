## Why

Teachers currently get no email when a submission lands in their review queue — the `execute_new_submission_task` handler is a dead `pass` loop because the `users` table has no email column, and no event is ever emitted on the `AWAITING_TEACHER_REVIEW` transition. Teachers must poll the dashboard to discover work. Naively emailing per-submission would flood a teacher's inbox when an entire group submits at once, so we need event-driven email plus time-window coalescing into a single digest.

## What Changes

- Add a nullable `email` column to `users` so teachers/admins can receive notifications.
- Emit a teacher-review notification at the two transitions into `AWAITING_TEACHER_REVIEW` (tests-then-teacher path in `check_tasks`, ai-then-teacher path in `review_tasks`), routed to the owning teacher of the submission's subject (fallback: active admins for ownerless startup-loaded subjects).
- Instead of sending immediately, enqueue each pending review into a new `teacher_notification_queue` table within the same transaction as the status change (reliable, idempotent per submission).
- Add a scheduled `flush_teacher_digests` job that groups unsent queue rows per teacher and sends **one digest email** listing all pending works. A teacher's batch flushes when EITHER a coalescing window has elapsed since their oldest pending item OR an eager count threshold is reached — so a whole-group submission becomes a single email, not N emails.
- Add a `teacher_digest` email template that renders 1..N works as a single list.
- Add config knobs: enable flag, window seconds, eager batch threshold, flush poll interval.
- Retire the broken `execute_new_submission_task` per-teacher loop (repurposed into the enqueue + digest path); `NEW_SUBMISSION` enum value retained to avoid a destructive PostgreSQL enum migration.

## Capabilities

### New Capabilities
- `teacher-notifications`: event-driven email to teachers when a submission enters their review queue, with per-teacher time-window/threshold coalescing into a single digest email to prevent inbox flooding.

### Modified Capabilities
<!-- No existing capability specs in openspec/specs/ change behavior. -->

## Impact

- **DB**: new migration adds `users.email` (nullable) + new `teacher_notification_queue` table. New model file; registered in `db/models/__init__.py`.
- **Code**: `workers/tasks/notification_tasks.py` (enqueue helper, retire dead handler), `workers/tasks/check_tasks.py` + `workers/tasks/review_tasks.py` (call enqueue on teacher transition), new `workers/scheduled/teacher_digest_processor.py`, `core/scheduler.py` (register job), `core/config.py` (new settings), `services/notifications/templates.py` (digest template), `db/models/user.py`.
- **Routing**: uses `Subject.owner_id` → `User`; advisory-lock pattern reused (new lock id) for single-flusher safety.
- **No breaking API changes.** Email only sends when a channel (SMTP/Resend/Brevo) is configured and the teacher has an email set; otherwise it logs and skips.
