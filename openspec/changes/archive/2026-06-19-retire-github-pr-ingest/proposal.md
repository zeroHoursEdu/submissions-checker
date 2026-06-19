## Why

The GitHub pull-request submission path is half-built and non-functional: the
`NOTIFY` stage posts results through `services/github/client.py`, whose methods all
raise `NotImplementedError`, and the `pull_tasks` → `review` (legacy) → `notify` chain
duplicates the working ZIP-upload flow. Carrying two ingest paths — one dead — adds
risk, dead code, and unused secret config. ZIP upload is the supported, exercised path,
so we standardize on it and remove the GitHub round-trip.

## What Changes

- **BREAKING**: Remove the `POST /webhooks/github` endpoint and its router. GitHub
  webhooks are no longer accepted.
- Remove the legacy ingest pipeline: `pull_tasks.py` (`execute_pull_task`),
  `notify_tasks.py` (`execute_notify_task`), and the legacy `execute_review_task`
  (the `REVIEW` stage). Keep the plugin-based `execute_ai_review_task`
  (`RUN_AI_REVIEW`) and the ZIP `RUN_CHECKS` path untouched.
- Remove `PULL` / `REVIEW` / `NOTIFY` dispatch branches from `outbox_processor.py`.
- Delete `services/github/` (`client.py`, `pr_handler.py`, `webhook_validator.py`) and
  the now-orphaned `utils/git.py` clone helper.
- Remove the GitHub webhook signature verification from `core/security.py` and the
  `github_webhook_secret`, `github_app_id`, `github_app_private_key_path`, and clone
  `workspace_dir` settings from config.
- Remove the `GITHUB_PR` / `GITLAB_MR` source-type code branches; ZIP becomes the only
  ingest. Enum **members** in `SubmissionSourceType` and the now-unused
  `OutboxEventType` (`PULL`/`REVIEW`/`NOTIFY`) are retained as deprecated to avoid a
  destructive Postgres enum migration on historical rows, but no code produces or
  dispatches them.

## Capabilities

### New Capabilities
<!-- none — this change is a removal/consolidation -->

### Modified Capabilities
- `auth-security`: Remove the "GitHub webhook authentication" requirement — the
  authenticated webhook endpoint no longer exists.
- `input-safety`: Remove the "Restricted git transport for clones" requirement — the
  only repository-clone code path (PR ingest) is deleted, so there is nothing left to
  clone.

## Impact

- **Endpoints**: `POST /webhooks/github` removed; `webhooks` router unregistered in
  `main.py`.
- **Workers**: `pull_tasks.py`, `notify_tasks.py`, legacy `execute_review_task`,
  `workers/tasks/__init__.py` exports, and `outbox_processor.py` dispatch branches.
- **Services/utils**: `services/github/` and `utils/git.py` deleted.
- **Config/security**: GitHub webhook + app settings and the HMAC-signature helper
  removed.
- **Models**: deprecated (retained) enum members documented; no schema migration.
- **Docs/specs**: `auth-security` and `input-safety` deltas; journey docs already
  describe ZIP-only ingest.
- **Tests**: any tests exercising the GitHub webhook / pull path are removed or
  retargeted; ZIP-upload flow and the rest of the suite must stay green.
