## Context

Event delivery uses a transactional outbox polled every 10s by `process_outbox_messages`. Notification handlers (`workers/tasks/notification_tasks.py`) run inside the outbox processor's DB transaction and send via `build_dispatcher(settings)` (SMTP/Resend/Brevo). Student emails work. Teacher emails do not: `users` has no email column, no event fires on the `AWAITING_TEACHER_REVIEW` transition, and `execute_new_submission_task` is a dead `pass` loop over all teachers.

Two transitions land a submission in teacher review:
- `check_tasks._advance_after_tests` → `test_passed_teacher` (review_mode `tests_then_teacher`) — `check_tasks.py:159`
- `review_tasks` → `ai_review_done_teacher` (next_step `teacher`) — `review_tasks.py:126`

Subject ownership: `Subject.owner_id → User` (nullable; null for startup-loaded subjects). Submission→teacher chain: `Submission → StudentAssignment → SubjectsAssignment → Subject.owner`.

The hard requirement: when a whole group submits at once, the teacher gets **one** email with a list, not N. So delivery must be decoupled from the per-submission event and coalesced over a time window.

## Goals / Non-Goals

**Goals:**
- Reliable, transactional enqueue of teacher-review notifications at the review transition.
- Coalesce per teacher into a single digest email via a time window plus an eager count threshold.
- Idempotent per (teacher, submission); safe under retries and multi-worker scheduling.
- Reuse existing dispatcher/channel/config/scheduler/advisory-lock patterns.

**Non-Goals:**
- In-app (badge/inbox) teacher notifications — email only this change.
- Per-teacher notification preferences/opt-out UI (a single global config toggle suffices for now).
- Notifying on non-teacher terminal states (tests_only auto-complete, quiz, AI-only completion).
- Backfilling teacher emails or building admin UI to edit them (column added; population is operational/out of scope).

## Decisions

### D1: Dedicated queue table, not immediate send, not per-event outbox round-trip
Add `teacher_notification_queue` (one row per pending review per teacher). The enqueue happens directly in the transition code path (which already runs inside the outbox processor's transaction) — atomic with the status change, no extra outbox hop.

- **Alternative — send inside `execute_new_submission_task`**: rejected; sends one email per submission → the exact flooding we must avoid.
- **Alternative — emit a `NEW_SUBMISSION` outbox message whose handler enqueues**: rejected as a redundant hop; the transition is already transactional, so we insert the queue row directly. `NEW_SUBMISSION` enum value is kept (no destructive PG enum migration) but no longer emitted; its dead handler is retired.

Queue schema:
- `id` BIGINT PK
- `teacher_id` BIGINT FK `users.id` ON DELETE CASCADE
- `submission_id` BIGINT FK `submissions.id` ON DELETE CASCADE
- `created_at` (TimestampMixin)
- `sent_at` `TIMESTAMPTZ NULL` — null = pending
- Unique `(teacher_id, submission_id)` for idempotency; enqueue uses INSERT ... ON CONFLICT DO NOTHING.
- Index `(teacher_id, sent_at, created_at)` to drive grouping/window queries.

Student name / assignment title / review URL are derived at flush time by joining `submission → student_assignment → student / subjects_assignment` — avoids denormalized staleness; CASCADE removes orphans.

### D2: Flush rule = window OR threshold (per teacher)
A scheduled `flush_teacher_digests` job (new file `workers/scheduled/teacher_digest_processor.py`) runs on `IntervalTrigger(seconds=teacher_digest_flush_interval)`, `max_instances=1`, guarded by a new advisory lock id (`7927`) mirroring the outbox processor.

Each run:
1. Load all pending rows (`sent_at IS NULL`), group by `teacher_id`.
2. For each teacher, flush when `count >= teacher_digest_max_batch` **OR** `now - min(created_at) >= teacher_digest_window_seconds`.
3. For a flushing teacher: skip if no channel configured or teacher has no email (log, leave rows pending). Otherwise build one digest email of all that teacher's pending rows, send via dispatcher, set `sent_at = now()` on those rows, commit.

The window gives a bounded max wait; the threshold flushes eagerly when volume spikes (whole-group burst) so a teacher isn't stuck waiting the full window with a large pile. Trickle submissions ride the window and still collapse into one email per window.

- **Alternative — fixed cron digest (e.g. every 5 min)**: rejected; either too laggy for single submissions or doesn't bound burst size. Window-since-oldest + threshold adapts to both.
- **Alternative — Redis sorted-set debounce**: rejected; DB-only keeps it transactional with enqueue and matches existing infra (no new dependency).

### D3: Routing = subject owner, fallback active admins
Teacher = `Subject.owner`. Ownerless startup subjects → fan out to active `ADMIN` users so notifications aren't silently dropped. Helper `_resolve_review_recipients(db, submission) -> list[User]`.

### D4: Config (core/config.py)
- `teacher_digest_enabled: bool = True`
- `teacher_digest_window_seconds: int = 120`
- `teacher_digest_max_batch: int = 25`
- `teacher_digest_flush_interval: int = 30`

When `teacher_digest_enabled` is False the job early-returns; rows still accumulate (re-enable drains them).

### D5: Digest template
`teacher_digest_template(teacher_name, items, dashboard_url)` where `items` is a list of `(student_name, assignment_title, review_url)`. Renders a header + bulleted list; handles 1..N uniformly (a single pending work is still a one-item digest).

## Risks / Trade-offs

- **Teacher emails unpopulated → nothing sends** → Mitigation: column added now; flush logs `teacher_digest_no_email` and leaves rows pending (re-drain once email is set). Non-fatal.
- **Window adds latency for a lone submission** (up to `window_seconds`) → Mitigation: window default 120s is small; tune via config. Acceptable trade for no-flood guarantee.
- **Flush partial failure** (send throws mid-batch) → Mitigation: only mark `sent_at` after a successful send; on failure rows stay pending and retry next interval (at-least-once; a rare duplicate digest is preferable to a lost one).
- **Unbounded pending growth while disabled / no email** → acceptable; bounded by submission volume and drained on re-enable. Could add a future reaper if needed.
- **Single advisory lock across flush + outbox are distinct ids** (7919 vs 7927) → no contention between the two jobs.

## Migration Plan

1. Alembic migration `0021`: add `users.email VARCHAR(255) NULL`; create `teacher_notification_queue` with FKs, unique `(teacher_id, submission_id)`, and the `(teacher_id, sent_at, created_at)` index.
2. Deploy code: enqueue calls at both transitions, new flush job registered in scheduler, config defaults, template.
3. Rollback: unregister job + revert migration (drop table, drop column). Queue rows are non-critical; dropping is safe.
4. No data backfill required.

## Open Questions

- Should ownerless-subject notifications go to all admins or be suppressed? Chosen: all active admins (safe default, avoids silent loss). Revisit if noisy.
- Future: teacher-side opt-out preferences and in-app notifications — deferred.
