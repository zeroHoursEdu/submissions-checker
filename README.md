# Submissions Checker

Automated checker for university programming coursework. Students upload a ZIP, the
platform runs the subject's check scripts in a locked-down Docker sandbox, and the
result flows through whatever the assignment asks for next: nothing, an AI code review,
a teacher review, or a proctored quiz. Grades, notifications, disputes and course
feedback are all in one place. The UI is Ukrainian.

## What it does

- **Subjects come from a config ZIP.** A teacher uploads `config.yml` + check scripts
  (see `docs/PLUGIN_AUTHORING.md`); there is no point-and-click editor by design.
- **Checks run in a sandbox** — no network, memory/CPU caps, one container per run,
  identical locally (`runner` CLI) and in production.
- **Review modes** per assignment: `tests_only`, `tests_then_ai`, `tests_then_teacher`,
  `tests_then_ai_then_teacher`, `tests_then_quiz`, plus check-free `quiz_only` /
  `quiz_then_teacher`.
- **Quizzes with proctoring**: tab/focus/copy/shortcut rules, optional webcam
  face-presence detection, evidence snapshots, per-question timers, question disputes,
  air-raid pause verified against alerts.in.ua.
- **Roles**: admin (creates teachers), teacher (owns subjects, enrols students, reviews),
  student (submits, takes quizzes). No self-registration.
- **Reliability**: transactional outbox + APScheduler; every side effect is a retried job.
- **Observability**: Prometheus metrics, Grafana dashboards, alerting (`docs/observability.md`).

Full route-by-route catalogue: `docs/feature_catalog.md`.

## Stack

FastAPI · SQLAlchemy 2 (async, asyncpg) · PostgreSQL 16 · Alembic · APScheduler ·
Jinja2 + Tailwind · S3-compatible storage (MinIO / LocalStack) · OpenAI or Anthropic
for AI review · Resend / Brevo / SMTP for email · structlog · Prometheus · uv · ruff · mypy.

## Run locally

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env            # set SECRET_KEY (openssl rand -hex 32)
make up                         # postgres + app + localstack + prometheus + grafana + alloy
make logs-app
```

Open http://localhost:8000. With `ENVIRONMENT=development` the seed migrations
(`0002`, `0003`) create demo accounts; the login page lists them.

Subjects for local testing live in `plugins/` (gitignored). Symlink a subject repo there
and upload its config ZIP from the teacher dashboard, or use `plugins/e2e_test`.

## Tests and quality

```bash
uv run --frozen --extra dev pytest -q        # unit + integration + functional (Docker needed)
make e2e                                     # Playwright / pytest-bdd against the compose stack
uv run --frozen ruff check src/ tests/
uv run --frozen ruff format --check src/ tests/
uv run --frozen mypy src/
```

Always pass `--frozen`; a bare `uv run` rewrites `uv.lock`. Layers and fixtures are
described in `tests/README.md`.

## Layout

```
src/submissions_checker/
  api/routes/      auth, student_portal, student_quiz, teacher_portal, teacher_disputes, admin, feedback, notifications, health
  core/            config, state_machine, scheduler, security, i18n, metrics, migrations
  services/        check_core, docker_sandbox, config_apply, grading, gradebook, quiz_scoring, quiz_regrade,
                   similarity, storage, ai/provider, air_raid/, notifications/
  workers/         scheduled/ (outbox, digest, metrics, stats)  tasks/ (checks, AI review, notifications)
  db/models/       one file per table; enums.py
  cli/runner.py    standalone check runner (shipped as the runner image)
templates/  i18n/uk.yml  alembic/  docker/  observability/  tests/  docs/
```

## Docs

| Doc | For |
|---|---|
| `docs/feature_catalog.md` | every feature, who can use it, routes, state machine |
| `docs/PLUGIN_AUTHORING.md` | writing a subject: config.yml, check/validate scripts, quiz block |
| `docs/anti-cheat.md` | quiz proctoring rules and presets |
| `docs/runner-contract.md` | stability contract for the `runner` CLI used by subject repos |
| `docs/deployment.md` | production stack (Caddy, two replicas, Watchtower, backups) |
| `docs/observability.md` | metrics, dashboards, alerts |
| `docs/student_journey_guide.md`, `docs/teacher_journey_guide.md`, `docs/admin_journey_guide.md` | narrative walkthroughs |
| `docs/known_bugs.md`, `docs/feature_audit.md` | what is broken, what is unfinished |

## License

See `LICENSE`.
