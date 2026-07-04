## Context

Seven independent, already-diagnosed bugs from `docs/known_bugs.md` (#2, #3, #4, #6, #7, #8, #11).
Each has a confirmed root cause and exact fix site; this design covers the handful of
non-obvious decisions rather than re-deriving root causes already nailed down in the bug report.

## Goals / Non-Goals

**Goals:**
- Fix each bug at its root cause, reusing existing helpers/patterns already established elsewhere
  in the same files (don't invent a second way to do ownership checks, error transitions, etc.).
- Keep each fix minimal and independently reviewable — no drive-by refactors.

**Non-Goals:**
- Bug #1 (plugin-autoloaded NULL `owner_id`) — already resolved by the
  `disable-plugin-autoloading` change; not part of this batch.
- Any bug not in the requested list (#5, #9, #10, etc., if they exist in `known_bugs.md`) —
  out of scope, not investigated here.
- Building a general alerting/observability system for outbox failures (bug #8) — just stop the
  silent-retry-to-nowhere behavior with a clear terminal log line; a full alerting pipeline is a
  separate concern.

## Decisions

**Bug #2 — redirect ADMIN to `/admin`, not a role-check ladder.** `admin.py`'s router is mounted
at prefix `/admin` with a bare `@router.get("")` index. Add `UserRole.ADMIN: "/admin"` as an
explicit branch in `_redirect_by_role` rather than restructuring it into an if/elif chain —
keeps the existing two-way ternary shape for the common case and makes the three-way mapping
explicit only where needed.

**Bug #3 — replace hand-rolled checks with `require_subject_access`, don't patch the checks.**
`require_subject_access(db, subject_id, current_user)` already does the `db.get`-equivalent
fetch, 404, and 403-with-ADMIN-bypass in one call, and 11 other routes in the same file already
call it this way. Rewriting each of the 3 hand-rolled blocks to call it removes the divergent
logic entirely rather than teaching it about ADMIN in three separate places (which would leave
the file with two ownership-check implementations to keep in sync going forward).

**Bug #4 — scope by `SubjectsStudents` join to the teacher's owned subjects, ADMIN sees all.**
Mirrors the existing ADMIN-bypass shape used at `teacher_review_submission`
(`if current_user.role != UserRole.ADMIN and subject.owner_id != current_user.user_id`), applied
as a query-level filter (`Subject.owner_id == current_user.user_id`, skipped entirely for ADMIN)
rather than a per-row Python filter, so pagination/counts stay correct.

**Bug #6 — reuse `_fail_validation`, which is already idempotent to the state it needs.**
`_fail_validation` only steps `PENDING → VALIDATING` via `start_validation` when
`submission.status == PENDING`; by the time `run_check()` can raise, the submission is already
`VALIDATING` (set before the call), so calling `_fail_validation(submission, str(exc))` after
catching `CheckExecutionError` naturally skips the already-done `start_validation` step and goes
straight to `validation_failed` — no change to `_fail_validation` itself needed, only a
try/except added around the `run_check()` call site.

**Bug #7 — generate a container name at the call site, not inside a shared constant.** Each
`docker run` invocation gets `--name f"submission-check-{uuid4().hex}"` added alongside the
existing `--rm`. On `TimeoutError`, run `docker kill <name>` (best-effort, swallow errors — the
container may have already exited) in addition to the existing `proc.kill()` local-process kill.
`--rm` still cleans up the container once `docker kill` stops it, so no leftover container state.

**Bug #8 — explicit branch for retired types, not a broader "unknown type" policy change.** Add
an explicit `elif message.event_type in (OutboxEventType.PULL, OutboxEventType.REVIEW,
OutboxEventType.NOTIFY):` branch before the final unknown-type fallback, logging
`outbox_retired_event_type_dropped` and marking the message `ERROR` immediately (no retry) via
the same state-setting path already used for the generic exception handler, rather than raising
and letting it retry `outbox_max_retries` times first — the retired types are permanently
undispatchable, so retrying them can never succeed and only delays the visible failure.

**Bug #11 — cap count while iterating, not after collecting.** `_read_output_dir` currently
iterates `output_dir.iterdir()` unbounded; add a running counter and `break` once
`MAX_OUTPUT_FILES` files have been added to the result, so a script that writes 100,000 tiny
files doesn't force reading and holding all of them before the cap is noticed. Files beyond the
cap are silently omitted from the result (matching today's silent-skip behavior for oversized
individual files) rather than raising — a check script's result.json still parses even if some
incidental debug files got dropped.

## Risks / Trade-offs

- [Risk] Bug #4's fix could newly reveal that some teacher relies (even accidentally) on seeing
  cross-tenant students today. → Mitigation: this is a privacy/scoping bug per the bug report
  itself; the correct behavior is scoping, and `subject-authorization`'s spec already states
  teachers should only see their own subjects' data — this fix brings the code in line with the
  already-stated intent.
- [Risk] Bug #7's `docker kill` could race with the container already having exited naturally
  right as the timeout fires. → Mitigation: swallow the kill command's failure (non-zero exit is
  expected/harmless in that race) rather than treating it as an error.
- [Risk] Bug #8's explicit retired-type list needs updating if more event types are retired later.
  → Mitigation: low-maintenance one-line addition when it happens; not worth generalizing for a
  hypothetical.
