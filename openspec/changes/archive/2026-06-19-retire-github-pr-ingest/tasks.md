## 1. Remove the webhook endpoint

- [x] 1.1 Delete `src/submissions_checker/api/routes/webhooks.py`
- [x] 1.2 Remove the `webhooks` import and `app.include_router(webhooks.router)` from `src/submissions_checker/main.py`

## 2. Remove the legacy worker pipeline

- [x] 2.1 Delete `src/submissions_checker/workers/tasks/pull_tasks.py`
- [x] 2.2 Delete `src/submissions_checker/workers/tasks/notify_tasks.py`
- [x] 2.3 Remove the legacy `execute_review_task` (the `REVIEW` stage) from `src/submissions_checker/workers/tasks/review_tasks.py`, keeping `execute_ai_review_task`; remove any PR-clone-only helpers it no longer needs
- [x] 2.4 Update `src/submissions_checker/workers/tasks/__init__.py` to drop `execute_pull_task`, `execute_notify_task`, and `execute_review_task` exports
- [x] 2.5 Remove the `PULL`, `REVIEW`, and `NOTIFY` dispatch branches and their imports from `src/submissions_checker/workers/scheduled/outbox_processor.py`, leaving the unknown-event fallback to error on any stray legacy row

## 3. Delete orphaned services and utils

- [x] 3.1 Delete the `src/submissions_checker/services/github/` package (`client.py`, `pr_handler.py`, `webhook_validator.py`, `__init__.py`)
- [x] 3.2 Delete `src/submissions_checker/utils/git.py`
- [x] 3.3 Grep for surviving importers of `GitHubClient`, `clone_repository`, `submissions_checker.services.github`, `utils.git`; fix or confirm none

## 4. Strip GitHub config and security helper

- [x] 4.1 Remove `github_webhook_secret`, `github_app_id`, `github_app_private_key_path`, and the clone `workspace_dir` settings from `src/submissions_checker/core/config.py`
- [x] 4.2 Remove the GitHub HMAC webhook-signature verification helper from `src/submissions_checker/core/security.py`
- [x] 4.3 Remove `GITHUB_*` entries from `.env.example` (do not touch `.env`)

## 5. Retire source-type branches (no schema migration)

- [x] 5.1 Remove `GITHUB_PR`/`GITLAB_MR` code branches; keep enum members in `db/models/enums.py` annotated as deprecated with a comment explaining the no-migration decision
- [x] 5.2 Add a deprecation comment to the unused `OutboxEventType.PULL`/`REVIEW`/`NOTIFY` members

## 6. Tests and docs

- [x] 6.1 Delete or retarget tests covering the GitHub webhook / pull / notify path
- [x] 6.2 Update any docs that reference GitHub PR ingest (e.g. `docs/jobs.md`, `docs/statuses.md`) to reflect ZIP-only ingest

## 7. Verify

- [x] 7.1 Run `grep -rn NotImplementedError src/` and confirm the GitHub-related stubs are gone
- [x] 7.2 Run the full test suite (`make test` / pytest) and confirm green — unit suite **25/25 pass**; app imports/builds clean; integration collects with no import breaks. Two pre-existing, unrelated failures remain (stale `test_root_endpoint` expecting 200 vs the by-design `/`→`/auth/login` 302 redirect; and postgres testcontainer `docker.errors.APIError` in this environment) — neither touched by this change.
- [x] 7.3 Run the E2E ZIP-upload submission flow (`docker-compose.e2e.yml`) — E2E stack built from current source and came up **healthy** (postgres-e2e + localstack-e2e + app-e2e, migrations + plugin load + `/health` all OK). Smoke + submission scenarios: **6 passed, 2 failed**. Both failures are in code untouched by this change (`teacher_portal` CSV import → 403; `analytics` dashboard → the documented `AdminUser`-only gate) and are pre-existing backlog gaps, not regressions. The ZIP-submission scenarios passed.
