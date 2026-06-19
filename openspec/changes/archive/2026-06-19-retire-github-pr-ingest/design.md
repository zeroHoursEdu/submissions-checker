## Context

Two submission-ingest paths coexist. The **ZIP-upload** path
(`student_portal.submit` → `RUN_CHECKS` → `check_tasks` → plugin `check_core` → optional
`RUN_AI_REVIEW`/teacher/quiz) is the production path, exercised by the E2E suite. The
**GitHub PR** path (`/webhooks/github` → `PULL`/`pull_tasks` clone → legacy `REVIEW`
→ `NOTIFY`/`notify_tasks` posting a comment) is incomplete: `services/github/client.py`
methods raise `NotImplementedError`, so `NOTIFY` cannot succeed. The PR path is dead
weight and carries unused GitHub secrets.

`outbox_processor.py` dispatches `PULL`, `REVIEW`, `NOTIFY`, plus the surviving
`RUN_CHECKS`, `RUN_AI_REVIEW`, `SUBMISSION_REVIEWED`, `QUIZ_RESULT`,
`DEADLINE_REMINDER`, `NEW_SUBMISSION`, `FEEDBACK_REQUEST_SENT`. `review_tasks.py` holds
both the legacy `execute_review_task` (PR `REVIEW`) and the current
`execute_ai_review_task` (`RUN_AI_REVIEW`).

## Goals / Non-Goals

**Goals:**
- Remove the GitHub PR ingest + result round-trip end to end.
- Keep the ZIP path, plugin AI review (`RUN_AI_REVIEW`), teacher review, and quiz flows
  fully intact.
- Remove unused GitHub config/secrets and the orphaned clone helper.
- Leave the database schema untouched (no migration).

**Non-Goals:**
- Touching the teacher subject-repo CI (the `base-subject-template` "push/PR builds the
  sandbox image" requirement is about a teacher's forked repo, not app ingest — unchanged).
- Reworking the outbox/scheduler mechanics or the surviving event handlers.
- GitLab MR support (never implemented; only the enum value existed).

## Decisions

**1. Retain deprecated enum members instead of removing them.**
`SubmissionSourceType.GITHUB_PR`/`GITLAB_MR` and `OutboxEventType.PULL`/`REVIEW`/`NOTIFY`
are PostgreSQL enum types. Removing a value from a PG enum is not transactional and
risks failing against historical rows. We keep the Python/PG members (annotated as
deprecated) and remove every code path that *produces* or *dispatches* them. After this
change no new row will use them. *Alternative considered:* a migration dropping the
values — rejected as destructive and unnecessary for the goal.

**2. `outbox_processor` rejects retired events rather than silently ignoring.**
The `PULL`/`REVIEW`/`NOTIFY` branches are deleted; the existing "unknown event type"
fallback handles any stray legacy row by raising/erroring (it moves to ERROR state),
which is visible and safe. *Alternative:* silently mark finished — rejected, hides bugs.

**3. Delete `utils/git.py` entirely.**
Its only live function (`clone_repository`) is called only from `pull_tasks`; its other
functions are `NotImplementedError` stubs. Nothing else imports it after removal.

**4. Delete `services/github/` entirely** (`client.py`, `pr_handler.py`,
`webhook_validator.py`). All are stubs or only referenced by the removed chain.

**5. Strip GitHub config + signature helper.** Remove `github_webhook_secret`,
`github_app_id`, `github_app_private_key_path`, and the clone `workspace_dir` from
`core/config.py`, and the HMAC verification helper from `core/security.py`. Grep first
to confirm no surviving importers.

## Risks / Trade-offs

- **Stray pending `PULL`/`REVIEW`/`NOTIFY` outbox rows after deploy** → they hit the
  unknown-event fallback and go to ERROR (not lost, just not processed). Mitigation:
  these events are transient and the dead path could never complete anyway; acceptable.
- **Removing a config field breaks `.env` parsing if it has extra keys** → pydantic
  settings ignore unknown env vars by default; leftover `GITHUB_*` env entries are
  harmless. No action needed beyond updating `.env.example`.
- **Hidden importer of a deleted module** → mitigate by grepping for every symbol
  (`clone_repository`, `GitHubClient`, `verify_github_signature`, `execute_pull_task`,
  `execute_notify_task`, `execute_review_task`) before and after deletion, and running
  the full test suite.
- **Tests referencing the webhook/PR path** → delete or retarget them; ensure ZIP-flow
  and E2E suites stay green.

## Migration Plan

1. Remove dispatch branches and `webhooks` router include; delete task/service/util
   modules; strip config + security helper.
2. Update `workers/tasks/__init__.py` exports and any imports.
3. Update `.env.example` and docs.
4. Run `grep -rn NotImplementedError src/` (count should drop) and the full test suite
   + E2E ZIP flow. Rollback = revert the change (schema untouched, so no data rollback).

## Open Questions

- None blocking. Confirm with the user that retaining (vs dropping) the deprecated enum
  members is acceptable — chosen here for migration safety.
