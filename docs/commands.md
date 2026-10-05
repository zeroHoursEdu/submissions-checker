# Commands

Every `make` target is a thin alias — the real logic lives in a script or a single
`docker compose` invocation, listed here. Run `make help` for the same list with less
detail. Every Python tool runs via `uv run --frozen --extra dev` (never a bare `uv run`,
which would rewrite `uv.lock`; never an activated venv).

## Which do I need?

- **Daily dev**: `make up`, `make logs-app`, `make test-unit`, `make lint`
- **Before pushing**: `make quality`, `make test`
- **Prod**: `make prod-backup-status`, `make prod-db`
- **Rarely**: `make e2e`, `make test-backup`, the observability targets

## Dev

| Target | What | When | Prerequisites | Destructive? |
|---|---|---|---|---|
| `install` | `uv sync --frozen --extra dev` | After pulling a change to `uv.lock` or `pyproject.toml` | `uv` installed | No |
| `setup` | Runs `vendor-assets`, then `dev_setup.sh`: installs locked deps, copies `.env.example` → `.env` if missing, starts the `postgres` container | First time cloning the repo | `uv`, Docker | No — never overwrites an existing `.env` |
| `vendor-assets` | `scripts/fetch_vendor_assets.py`: downloads the MediaPipe face-landmarker bundle and the Tailwind play-CDN build into `static/vendor/` | Rarely by hand — `up` and `e2e` already depend on it | Network access (first run only; idempotent and hash-checked after) | No |
| `up` | `docker compose up -d` (postgres, minio, minio-init, app on :8000, hot reload) | Start the dev stack | Docker; a `.env` file (`cp .env.example .env`) | No — named volumes (`postgres_data`, `minio_data`) persist |
| `down` | `docker compose down` | Stop the dev stack | Dev stack running | No — data volumes are kept (use `docker compose down -v` by hand to wipe them) |
| `logs` | `docker compose logs -f` (all services) | Tail everything at once | Dev stack running | No |
| `logs-app` | `docker compose logs -f app` | Tail just the app | Dev stack running | No |
| `db-shell` | `psql` inside the `postgres` container | Poke at dev data by hand | Dev stack running | Whatever you type at the prompt is real — you're in the dev DB, not a scratch copy |
| `health` | `curl`s `/health` and `/health/ready` | Quick liveness/readiness check | App container running and listening on :8000 | No |
| `api-docs` | Opens `http://localhost:8000/docs` (Swagger UI) in a browser, or prints the URL headlessly | Poke at the API interactively | App running; a browser (falls back to printing the URL) | No |

## Test

| Target | What | When | Prerequisites | Destructive? |
|---|---|---|---|---|
| `test` | `pytest` over the whole suite (unit + integration + functional) with coverage (`--cov`, `htmlcov/`, `coverage.xml`) | Before opening a PR; CI runs the equivalent | Docker (testcontainers spin up Postgres/Redis); ~6 min | No |
| `test-unit` | `pytest tests/unit -q` | While iterating — no Docker, seconds | Nothing but `uv` | No |
| `test-integration` | `pytest tests/integration -q` against a testcontainers Postgres (+ Redis) | Testing the outbox, scheduled workers, config-apply pipeline | Docker | No |
| `test-functional` | `pytest tests/functional -q` — the real FastAPI app over ASGI, full auth stack, testcontainers Postgres | Testing routes, permissions, auth | Docker | No |
| `test-ops` | `bash tests/ops/test_ops_scripts.sh` — offline: fakes `ssh` on `PATH` and checks help text, missing-config errors, and the exact remote command lines for every `scripts/ops/*.sh` script | Changing anything in `scripts/ops/` | Nothing (no real network, no real prod) | No |
| `test-backup` | `bash tests/ops/backup_roundtrip.sh` — seeds data, backs up, destroys it, restores, and asserts, in its own compose project `subchk-backup-test` (own volumes, no host ports) | Changing `docker/backup/backup.sh` or the backup compose services | Docker; builds the backup image; ~1 min | No to your dev stack — it never touches `docker-compose.yml`'s containers or volumes; it does create/tear down its own |

## Quality

| Target | What | When | Prerequisites | Destructive? |
|---|---|---|---|---|
| `lint` | `ruff check src tests` | Before pushing | `uv` | No |
| `lint-fix` | `ruff check --fix src tests` | Cleaning up lint findings you don't want to fix by hand | `uv` | Rewrites files (autofix) |
| `format` | `ruff format src tests` | After writing code | `uv` | Rewrites files |
| `format-check` | `ruff format --check src tests` | CI / before pushing (no writes) | `uv` | No |
| `type-check` | `mypy src` | Before pushing | `uv` | No |
| `shellcheck` | Runs `koalaman/shellcheck:stable` in Docker over `docker/backup/backup.sh`, `docker/minio/init.sh`, `scripts/ops/*.sh`, `tests/ops/*.sh`, `dev_setup.sh` | Changing any shell script in the repo | Docker (pulls the shellcheck image on first use) | No |
| `quality` | `lint` + `format-check` + `type-check` + `shellcheck` | Everything CI checks except the test suite | Union of the above | No |
| `clean` | Removes `__pycache__`, `.pytest_cache`, `.ruff_cache`, `htmlcov/`, `.coverage`, `coverage.xml` | Reclaiming disk / forcing a clean rebuild of caches | None | Deletes generated files only — nothing tracked in git |

## E2E

| Target | What | When | Prerequisites | Destructive? |
|---|---|---|---|---|
| `e2e` | Brings up `docker-compose.e2e.yml` (`--build --wait`), runs `pytest-bdd` against it, tears it down even on failure. Accepts `TAGS=@tag`, `SCENARIO="name"`, `FILE=path`, `HEADED=1` | Before merging a change that touches templates/JS or a full user flow | Docker; `uv run --frozen --extra e2e` needs Playwright installed (`playwright install chromium firefox`, see `tests/README.md`); ~3.5 min | No to the dev stack — it's a separate compose file/ports (`:8001`, `:5435`); always torn down at the end |
| `e2e-up` | Starts the e2e stack only, no tests | Debugging a scenario by hand against `:8001` | Same as `e2e` | No — but you must `make e2e-down` yourself |
| `e2e-down` | Stops the e2e stack | Cleaning up after `e2e-up` | — | No |
| `e2e-logs` | `docker compose -f docker-compose.e2e.yml logs -f app-e2e` | Debugging a running e2e stack | e2e stack running | No |

## LLM judge sidecar (`docs/deployment.md#classroom-ingest-and-nightly-llm-grading`)

| Target / command | What | When | Prerequisites | Destructive? |
|---|---|---|---|---|
| `make llm-judge-smoke` | `scripts/ops/llm-judge-smoke.sh`: `GET /health`, then POSTs `tests/fixtures/sample.pdf` to `/grade` of the sidecar at `$LLM_JUDGE_URL` (default `http://localhost:8090`; `--url`/`--file` override) | After starting or re-logging-in the sidecar; checking that the pinned CLI still works | `LLM_JUDGE_TOKEN` in the environment (same value as the sidecar); locally a sidecar from `docker compose --profile llm up -d --build llm-judge` | No (one real model call, which uses the subscription quota) |
| `docker compose --profile llm run --rm -it llm-judge claude` | One-time login of the local sidecar: in the CLI run `/login` | First use of the dev sidecar | The `llm` profile | No |
| `scripts/ops/prod-compose.sh up -d --build llm-judge` | Builds the prod sidecar from the repo checkout (Watchtower never touches it) and starts it | First rollout and after any change under `docker/llm-judge/` | `PROD_SSH`/`PROD_DIR`; updated checkout on the host | Recreates the sidecar only; the login volume survives |
| `scripts/ops/prod-compose.sh run --rm -it llm-judge claude` | One-time login of the prod sidecar: `/login`, approve in the browser, paste the code, `/exit` | First rollout; again if `/health` reports `logged_in: false` | `PROD_SSH`/`PROD_DIR`; a Claude subscription account | No |

## Observability (`docs/observability.md`)

| Target | What | When | Prerequisites | Destructive? |
|---|---|---|---|---|
| `observability-up` | `docker compose --profile observability up -d prometheus loki grafana alloy` next to the app, passing the resolved compose project name so a worktree's stack ships its own logs | Trying out a dashboard/alert or log-pipeline change locally before pushing to Grafana Cloud | Dev stack (`app`) already up for Alloy to scrape/tail; Docker | No |
| `observability-down` | Stops and removes those four containers | After `observability-up` | — | No |
| `alloy-logs` | Follows Alloy's log — where a rejected metrics/logs push would show up | Debugging a metrics/label or log-shipping problem | `observability-up` running | No |
| `dashboards-json` | Regenerates `observability/grafana/*.json` and `alerting/*.yaml` from the Python builders | After changing dashboard/alert source in `observability/grafana/build_*.py` | `uv` (stdlib only, no network) | Overwrites the generated JSON/YAML files in the working tree (never hand-edit them directly) |
| `dashboards` | `push.py dashboards` — pushes the four committed dashboards (Technical, Goals, Quiz investigation, Logs) to Grafana Cloud, folder `subchk` only | Publishing a dashboard change | `GRAFANA_URL` + `GRAFANA_API_TOKEN` in `.env` | Overwrites the `subchk` folder's dashboards on the real Grafana Cloud stack (touches nothing outside that folder) |
| `alerting` | `push.py alerting` — pushes the Telegram contact point, notification policy route, and the `subchk` alert rule group (now 6 rules: the original 4 Prometheus ones plus `QuizForceFailSpike`/`OutboxDead` from logs) | Publishing an alerting change | `GRAFANA_URL` + `GRAFANA_API_TOKEN` + `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in `.env` | Overwrites the `subchk-telegram` contact point and `subchk` rule group on Grafana Cloud (touches nothing else on the stack) |

## Prod ops

These need production access. Set `PROD_SSH` and `PROD_DIR` in the environment, or copy
`scripts/ops/prod.env.example` to `ssh/prod.env` (gitignored) and fill it in. Every
`scripts/ops/*.sh` script also has its own `--help`.

### Make targets

| Target | What | When | Prerequisites | Destructive? |
|---|---|---|---|---|
| `prod-db` | `scripts/ops/connect-to-prod-db.sh` — interactive **read-only** `psql` session on the prod database over SSH | Looking something up in prod data | `PROD_SSH`/`PROD_DIR` | No (the session itself sets `default_transaction_read_only=on`) |
| `prod-backup` | `scripts/ops/run-prod-backup.sh --now` — triggers an out-of-schedule backup | Right before a risky deploy or migration | `PROD_SSH`/`PROD_DIR`; the `backup` service enabled on prod (`COMPOSE_PROFILES=backup`) | No — only adds a new backup |
| `prod-backup-status` | `scripts/ops/run-prod-backup.sh --status` — age of the last successful backup; exits non-zero if older than 12h | Sanity check before/after a deploy, or in CI | `PROD_SSH`/`PROD_DIR` | No |

### Scripts without a make target

| Script | What | Prerequisites | Destructive? |
|---|---|---|---|
| `scripts/ops/prod-compose.sh <docker compose args...>` | Thin wrapper: runs `docker compose -f docker-compose.prod.yml --env-file .env <args>` in `$PROD_DIR` on `$PROD_SSH` — the same plumbing the scripts below use, for anything they don't cover (e.g. recreating `alloy` after changing `.env`, `ps`, ad-hoc `logs`). | `PROD_SSH`/`PROD_DIR` | Only if the args are — `ps`/`logs` are read-only, `up -d --force-recreate <service>` recreates that one service |
| `scripts/ops/connect-to-prod-db.sh [--write] [--tunnel [PORT]] [-c SQL]` | With no flags: read-only interactive `psql`. `-c SQL` runs one statement (SQL travels on stdin, never as a shell argument, so multi-line/quoted statements survive the SSH hop byte-for-byte). `--write` drops the read-only guard. `--tunnel [PORT]` forwards `localhost:PORT` (default 15432) to prod Postgres for a GUI client. | `PROD_SSH`/`PROD_DIR` | Only with `--write` — otherwise no |
| `scripts/ops/run-prod-backup.sh [--now\|--status\|--list\|--logs]` | `--list` prints available dump stamps oldest-first (feed one to restore); `--logs` follows the backup container's log. `--now`/`--status` are the make targets above. | `PROD_SSH`/`PROD_DIR`; backup service enabled | No |
| `scripts/ops/restore-prod-from-backup.sh <STAMP\|latest> [--yes]` | **Replaces the production database and MinIO bucket** with the given backup. Stops the app, saves the current DB to `pre-restore/<now>.dump` on the remote first, `pg_restore`s in one transaction, syncs the bucket (previous bucket contents also saved under `pre-restore/`), restarts the app (even if the restore itself failed). Requires typing `restore` to confirm unless `--yes` is passed. | `PROD_SSH`/`PROD_DIR` | **Yes — this is the one that can lose data if you pick the wrong stamp.** Has **no `make` target on purpose**: a one-word alias like `make prod-restore` makes the most dangerous command in the repo too easy to fire by muscle memory or tab-completion. Typing out the script name and a stamp is the point. |
| `python -m submissions_checker.cli.migrate_uploads` (run via `docker compose -f docker-compose.prod.yml --env-file .env run --rm app python -m submissions_checker.cli.migrate_uploads` on prod) | One-time backfill: copies legacy local submission ZIPs into object storage. Idempotent (already-uploaded objects are skipped) and also lists subjects whose config predates stored config archives. | Prod access (runs inside the `app` container/image) | No — additive; safe to re-run |

## Removed targets and their replacements

The old Makefile had several targets that either silently depended on an activated
virtualenv, bypassed `uv.lock`, or (in one case) didn't work at all. They're gone; here's
what replaces them:

| Removed | Replacement | Why |
|---|---|---|
| `venv`, `activate` | `make install` (`uv sync --frozen`) | `uv run ...` never needs an activated venv; `uv sync` creates `.venv` itself. |
| `install` (old: `uv pip install -e ".[dev]"`) | `make install` (`uv sync --frozen --extra dev`) | The old form ignored `uv.lock`, so two people running `make install` could get different dependency versions. |
| `dev` (`docker compose up app`, app only) | `make up` | Starting the app alone was never actually useful — it depends on postgres and minio being up too, and `up` already starts everything with hot reload. |
| `test-watch` (`pytest --watch`) | *(none)* | `--watch` isn't a real pytest flag without the `pytest-watch` plugin, which isn't a project dependency — this target has always failed with `unrecognized arguments: --watch`. Re-run `make test-unit` by hand, or add `pytest-watch` yourself if you want this. |
| `shell` (`docker compose run --rm app python`) | *(none — run the `docker compose` command directly if you need it)* | Rarely used enough to warrant an alias; kept out of `.PHONY` to keep the target list to things people actually reach for. |
| `build`, `rebuild` | `docker compose build`, `docker compose build --no-cache` | `up` already rebuilds when Dockerfile/context changes; a bare `build`/`rebuild` alias added a name to remember for something one line of `docker compose` already does. |
| `ps` | `docker compose ps` | Same reasoning — a one-line passthrough with no logic to hide. |
| `e2e-headed` | `make e2e HEADED=1` | Folded into the `e2e` target's variables instead of duplicating the whole recipe. |

Everything else kept its old name and behaviour (allowing for the `uv run --frozen
--extra dev` prefix now applied consistently — some Python targets used to call `ruff`/
`mypy`/`pytest` directly, which only worked with an activated venv or global install).
