# Junior Dev Handover Checklist

Topics to walk through when handing this project over. Work through them roughly in order — each section builds on the previous one.

---

## 1. Project purpose
- [ ] What the system does: automated code review of student GitHub pull requests, followed by a personalised quiz
- [ ] Who the actors are: students, instructors, GitHub, OpenAI, Google Forms, Brevo
- [ ] High-level flow: student opens PR → webhook → clone → AI review → Google Form quiz → student submits quiz → pass/fail email

---

## 2. Local setup
- [ ] Python version and virtual environment (`.venv`, `uv` or `pip`)
- [ ] Copy `.env.example` → `.env` and fill in required values
- [ ] Required env vars: `DATABASE_URL`, `GITHUB_WEBHOOK_SECRET`, `SECRET_KEY`, `OPENAI_API_KEY`
- [ ] Start PostgreSQL locally (or via Docker: `docker compose up`)
- [ ] Run the app: `uvicorn submissions_checker.main:app --reload`
- [ ] Verify health check: `GET /health`
- [ ] Swagger UI at `/docs`

---

## 3. Tech stack
- [ ] **FastAPI** — async HTTP framework, lifespan context manager pattern
- [ ] **SQLAlchemy** (async) — ORM for DB models; `AsyncSession` everywhere
- [ ] **PostgreSQL** — primary datastore; also used for advisory locks
- [ ] **pydantic-settings** — typed config loaded from `.env` (`core/config.py`)
- [ ] **APScheduler** — in-process background job scheduler (10-second poll loop)
- [ ] **structlog / logging** — structured logging (`get_logger(__name__)`)

---

## 4. Configuration system
- [ ] `core/config.py`: `Settings` class backed by pydantic-settings; reads from `.env`
- [ ] `get_settings()` is cached with `@lru_cache` — call it anywhere, no DI needed
- [ ] Key knobs: `SCHEDULER_ENABLED`, `OUTBOX_BATCH_SIZE`, `OUTBOX_MAX_RETRIES`, `WORKSPACE_DIR`
- [ ] `SCHEDULER_ENABLED=false` runs an API-only instance (no background worker)

---

## 5. Application startup sequence (`main.py`)
- [ ] `lifespan()` context manager: runs on startup and shutdown
- [ ] Startup order: run migrations → init DB pool → start scheduler
- [ ] Shutdown order: stop scheduler (waits for in-flight jobs) → close DB
- [ ] Why migrations run on startup: keeps schema in sync without a separate deploy step

---

## 6. Database migrations
- [ ] Custom raw-SQL migration system in `core/migrations.py` (not Alembic)
- [ ] Migration files live in `migrations/sql/` — numbered, ordered, run once
- [ ] `schema_migrations` table tracks which files have already run
- [ ] To add a migration: create `NNN_description.sql` in `migrations/sql/`

---

## 7. Database models
- [ ] `db/models/base.py` — `TimestampMixin` (created_at, updated_at) on all models
- [ ] `db/models/outbox.py` — `OutboxMessage`: the message queue table
- [ ] `db/models/submission.py` — `Submission`: one row per student PR
- [ ] `db/models/enums.py` — all enums: `SubmissionStatus`, `OutboxState`, `OutboxEventType`
- [ ] `db/session.py` — `get_db_session()` async context manager for background tasks
- [ ] `core/database.py` — `init_db()` / `close_db()` manage the connection pool

---

## 8. Transactional outbox pattern
- [ ] Why it exists: reliably decouple "write business data" from "trigger async work"
- [ ] Inserting an `OutboxMessage` in the same transaction as business data = atomicity guarantee
- [ ] The processor picks up `pending` messages and runs their handlers
- [ ] On success: message state → `finished`; on failure: state → `error`, increments `retry_count`
- [ ] Max retries controlled by `OUTBOX_MAX_RETRIES`; after that it stays `error` and needs manual intervention
- [ ] Read the state machine in `src/submissions_checker/core/state_machine.py` and the diagram in `docs/feature_catalog.md` §4

---

## 9. GitHub webhook entry point (`api/routes/webhooks.py`)
- [ ] `POST /webhooks/github` receives all GitHub webhook payloads
- [ ] `core/security.py` — HMAC-SHA256 signature validation against `GITHUB_WEBHOOK_SECRET`
- [ ] Only `pull_request` events with action `opened` or `synchronize` are handled; everything else returns 200 and is ignored
- [ ] Handler extracts fork URL, branch, commit SHA, repo names → writes a `PULL` `OutboxMessage`
- [ ] Returns immediately; all real work is async
- [ ] `POST /webhooks/quiz-submission` — receives quiz scores from Google Apps Script; enqueues `NOTIFY_QUIZ_RESULT`; rejects updates if the submission already has a passing score

---

## 10. Background processor (`workers/scheduled/outbox_processor.py`)
- [ ] Called every 10 seconds by APScheduler
- [ ] Acquires a PostgreSQL advisory lock before doing anything — prevents concurrent runs across multiple app instances
- [ ] Fetches a batch of `pending` / retryable `error` messages (ordered by `created_at`)
- [ ] Dispatches each to the correct handler function based on `event_type`
- [ ] Each handler runs inside the same DB session → commits atomically with the message state update
- [ ] `core/scheduler.py` — `init_scheduler()`, `start_scheduler()`, `shutdown_scheduler()`
- [ ] APScheduler uses `max_instances=1` — if a job (e.g. git clone) takes longer than 10 s, the next tick is skipped rather than queued; skipped ticks are simply dropped

---

## 11. Job pipeline
- [ ] Read `src/submissions_checker/core/scheduler.py` for the four scheduled jobs and `workers/scheduled/outbox_processor.py` for dispatch
- [ ] **PULL** (`workers/tasks/pull_tasks.py`) — fully implemented
- [ ] **REVIEW** (`workers/tasks/review_tasks.py`) — fully implemented; calls OpenAI, uses lecture knowledge from DB
- [ ] **GENERATE_QUIZ** (`workers/tasks/generate_quiz_tasks.py`) — fully implemented; calls Google Apps Script
- [ ] **NOTIFY** (`workers/tasks/notify_tasks.py`) — fully implemented; posts quiz link to GitHub PR
- [ ] **NOTIFY_QUIZ_RESULT** (`workers/tasks/notify_quiz_result_tasks.py`) — fully implemented; sends email via Brevo

---

## 12. Services layer
- [ ] `services/github/client.py` — GitHub REST API client (post comments, update commit status)
- [ ] `services/github/webhook_validator.py` — webhook HMAC validation logic
- [ ] `services/github/pr_handler.py` — parses raw webhook payload into structured data
- [ ] `services/ai/client.py` — wraps OpenAI (or compatible) API
- [ ] `services/ai/code_reviewer.py` — builds prompts and parses AI responses
- [ ] `services/notifications/` — notification channel abstraction; currently only Brevo is supported
- [ ] `utils/git.py` — `clone_repository()`, `remove_repository()` shell helpers (30-second timeout on clone)

---

## 13. API routes (other than webhooks)
- [ ] `api/routes/health.py` — `GET /health` liveness check
- [ ] `api/routes/users.py` — user management (CRUD)
- [ ] `api/dependencies.py` — FastAPI dependency injection helpers (DB session, settings)

---

## 14. What is NOT yet implemented
- [ ] Tests — skeleton files exist in `tests/` but are mostly empty
- [ ] GitHub App authentication — only webhook secret auth is in place; App-based token auth is stubbed in config but not wired up

---

## 15. Email notifications (Brevo)

Email is sent to students after they complete the quiz. **The only supported provider is Brevo** — SMTP is blocked by most cloud platforms (including Railway).

**Brevo setup:**

1. Sign up for free at [brevo.com](https://brevo.com) (300 emails/day free forever, no credit card)
2. Go to **Senders & IP** → add your sender email → confirm via the verification email they send you
3. Go to top-right account menu → **Profile & Plan** → left sidebar **API Keys** → **Generate a new API key** → copy it

**Required env vars:**
```
BREVO_API_KEY=xkeysib-...
BREVO_FROM_ADDRESS=your-sender-email@example.com   # must be verified in Brevo
```

Key points:
- `BREVO_FROM_ADDRESS` must be a **verified sender** in Brevo or emails will silently not be sent
- Do **not** use a Gmail account with "bot" in the name — Google will ban it for automated sending
- If `BREVO_API_KEY` is not set, the email channel is silently skipped (no error)

---

## 16. Deployment: know your backend URL first

Before deploying to any environment you need a stable public URL for the backend. Everything else depends on it.

**Step 1 — Deploy the Google Apps Script**
- Open [script.google.com](https://script.google.com), create a new standalone project
- Paste the contents of `scripts/quiz_form.gs`
- Deploy → New deployment → type "Web app", execute as yourself, access "Anyone"
- Copy the deployment URL and set it in env:
```
GOOGLE_SCRIPT_URL=https://script.google.com/macros/s/<deploy-id>/exec
```

**Step 2 — Determine the backend URL** (e.g. `https://your-app.up.railway.app`)

**Step 3 — Set it in config**
```
BASE_URL=https://your-app.up.railway.app
```
This value is used to build the quiz-submission callback URL passed to Google Apps Script (`/webhooks/quiz-submission?submission_id=N`).

**Step 4 — Register it as the GitHub webhook target**
In the assignment repository → Settings → Webhooks → Add webhook:
- Payload URL: `https://your-app.up.railway.app/webhooks/github`
- Content type: `application/json`
- Secret: same value as `GITHUB_WEBHOOK_SECRET` in env
- Events: "Pull requests" only

**Step 5 — Deploy**
Migrations run automatically on startup. Verify with `GET /health`.

---

## 17. Migrating to a different cloud provider

The app is a single Docker container defined in `docker/app/Dockerfile`. It needs:
- A PostgreSQL database
- Outbound HTTPS access (port 443) to: GitHub API, OpenAI API, Google Apps Script, Brevo API
- An inbound public HTTPS URL for GitHub webhooks and Google Apps Script callbacks

**Steps for any cloud migration:**

1. **Provision a PostgreSQL database** on the new provider (or keep the old one and just point `DATABASE_URL` at it).

2. **Set all environment variables** on the new provider — copy every var from the table below.

3. **Update `BASE_URL`** to the new provider's public URL.

4. **Update the GitHub webhook** payload URL to the new URL (repository Settings → Webhooks).

5. **Update the Google Apps Script** — if the callback URL domain changed, redeploy the Apps Script with the new `BASE_URL` value, or just ensure `BASE_URL` is set correctly and the script reads the `callback_url` parameter dynamically (which it does).

6. **Deploy the Docker image** using `docker/app/Dockerfile`. The build command is: `docker build -f docker/app/Dockerfile .`

7. **Verify** with `GET /health` and trigger a test PR.

**Complete environment variable reference:**

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | Yes | PostgreSQL DSN: `postgresql+asyncpg://user:pass@host:5432/db` |
| `SECRET_KEY` | Yes | Random string ≥ 32 chars for internal signing |
| `GITHUB_WEBHOOK_SECRET` | Yes | HMAC secret shared with GitHub webhook config |
| `GITHUB_TOKEN` | Yes | Personal access token for posting PR comments |
| `OPENAI_API_KEY` | Yes | OpenAI API key for AI review |
| `OPENAI_MODEL` | No | Default: `gpt-4` |
| `GOOGLE_SCRIPT_URL` | Yes | Google Apps Script deployment URL |
| `BASE_URL` | Yes | This app's public HTTPS URL (no trailing slash) |
| `BREVO_API_KEY` | Yes | Brevo API key for sending student emails |
| `BREVO_FROM_ADDRESS` | Yes | Verified sender email in Brevo |
| `QUIZ_PASS_THRESHOLD` | No | Minimum score to pass (default: `6`) |
| `ENVIRONMENT` | No | `development` / `production` (default: `development`) |
| `SCHEDULER_ENABLED` | No | `true` to run background jobs (default: `true`) |
| `OUTBOX_BATCH_SIZE` | No | Messages processed per cycle (default: `1`) |
| `OUTBOX_MAX_RETRIES` | No | Max retries before a message stays in `error` (default: `5`) |
| `OUTBOX_RETRY_BACKOFF_SECONDS` | No | Seconds before a failed message is retried (default: `60`) |
| `WORKSPACE_DIR` | No | Directory for cloned repos (default: `/tmp/repos`) |

---

## 18. Key operational concerns
- [ ] `WORKSPACE_DIR` needs adequate disk space and must be writable; old clones are deleted before re-cloning on PR update. On ephemeral cloud instances (Railway, Fly.io) this is an in-memory tmpfs — suitable for short-lived clones, but lost on restart
- [ ] Outbox messages in `error` state after max retries need manual review/reset via SQL
- [ ] Running multiple app instances: set `SCHEDULER_ENABLED=false` on API instances and `SCHEDULER_ENABLED=true` only on dedicated worker instances — the advisory lock provides safety but a single worker instance is simpler
- [ ] Railway blocks outbound SMTP (ports 25, 465, 587) — always use Brevo (HTTPS port 443)
- [ ] Logs are structured (JSON-friendly) — use `event` key to search

---

## 19. Suggested reading order for the code
1. `docs/feature_catalog.md` + `core/state_machine.py`
2. `core/config.py`
3. `main.py`
4. `db/models/outbox.py` + `db/models/submission.py`
5. `api/routes/webhooks.py`
6. `workers/scheduled/outbox_processor.py`
7. `workers/tasks/pull_tasks.py`
8. `workers/tasks/review_tasks.py`
9. `workers/tasks/generate_quiz_tasks.py`
10. `workers/tasks/notify_tasks.py`
11. `workers/tasks/notify_quiz_result_tasks.py`
