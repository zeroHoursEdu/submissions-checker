## 1. Data model & migration

- [x] 1.1 Add nullable `email: Mapped[str | None]` (`String(255)`) to `db/models/user.py`
- [x] 1.2 Create `db/models/teacher_notification_queue.py` with `TeacherNotificationQueue` (id PK, teacher_id FK users CASCADE, submission_id FK submissions CASCADE, sent_at nullable TIMESTAMPTZ, TimestampMixin), unique `(teacher_id, submission_id)`, index `(teacher_id, sent_at, created_at)`
- [x] 1.3 Register the new model in `db/models/__init__.py`
- [x] 1.4 Add Alembic migration `0021`: add `users.email` column; create `teacher_notification_queue` table with FKs, unique constraint, and index. Include downgrade (drop table, drop column)

## 2. Config

- [x] 2.1 Add settings to `core/config.py`: `teacher_digest_enabled: bool = True`, `teacher_digest_window_seconds: int = 120`, `teacher_digest_max_batch: int = 25`, `teacher_digest_flush_interval: int = 30`

## 3. Enqueue on review transition

- [x] 3.1 In `workers/tasks/notification_tasks.py` add `_resolve_review_recipients(db, submission) -> list[User]` (subject owner; fallback active ADMIN users)
- [x] 3.2 Add `enqueue_teacher_review_notification(db, submission)` that resolves recipients and inserts one `TeacherNotificationQueue` row per teacher using INSERT ... ON CONFLICT DO NOTHING (idempotent per teacher+submission)
- [x] 3.3 Call enqueue from `check_tasks._advance_after_tests` in the `tests_then_teacher` branch (after the `test_passed_teacher` transition)
- [x] 3.4 Call enqueue from `review_tasks` in the `ai_review_done_teacher` branch (after the transition, `next_step == "teacher"`)
- [x] 3.5 Retire the dead `execute_new_submission_task` body (remove the no-op teacher loop); keep `NEW_SUBMISSION` enum value and a deprecated no-op handler so the dispatch branch stays harmless

## 4. Digest template

- [x] 4.1 Add `teacher_digest_template(teacher_name, items, dashboard_url)` to `services/notifications/templates.py` rendering a single email listing all `(student_name, assignment_title, review_url)` items (handles 1..N)

## 5. Flush job

- [x] 5.1 Create `workers/scheduled/teacher_digest_processor.py` with `flush_teacher_digests()`: advisory lock (id 7927), early-return when `teacher_digest_enabled` is False
- [x] 5.2 Load pending rows (`sent_at IS NULL`), group by teacher_id, join submission→student/assignment for display fields
- [x] 5.3 Per teacher, flush when `count >= teacher_digest_max_batch` OR `now - oldest.created_at >= teacher_digest_window_seconds`
- [x] 5.4 Skip (log, leave pending) when no channel configured or teacher has no email; otherwise send one digest via `build_dispatcher(settings)` and set `sent_at` on the included rows; commit
- [x] 5.5 Register the job in `core/scheduler.py` on `IntervalTrigger(seconds=teacher_digest_flush_interval)`, `max_instances=1`, id `"teacher_digest_processor"`

## 6. Tests & verification

- [x] 6.1 Test enqueue routes to subject owner and to admins when ownerless; idempotent on duplicate enqueue
- [x] 6.2 Test flush coalesces multiple pending rows for one teacher into a single email (whole-group case)
- [x] 6.3 Test flush triggers on threshold before window, and on window for a trickle; sent rows are not re-sent
- [x] 6.4 Test no-email / no-channel / disabled paths skip without error and leave rows pending
- [x] 6.5 Run migration up/down and full test suite (`--extra dev --with redis`)
