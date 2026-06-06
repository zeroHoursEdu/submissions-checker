## Context

The app is a FastAPI server rendered with Jinja2 templates. All user flows are HTML-form-driven (no SPA); authentication uses signed JWT cookies. The dev stack runs via Docker Compose. Existing tests are unit/integration only (pytest + httpx). There is a `docker-compose.test.yml` that may be extended or superseded by a dedicated E2E compose file.

## Goals / Non-Goals

**Goals:**
- Full browser E2E coverage of the happy path and key sad paths for teacher and student portals
- Gherkin feature files that serve as living documentation of user flows
- Isolated test database (never touches dev or prod data)
- Easy local execution with a single `make e2e` command and clear options (headed, tags, specific scenario)
- Validate: teacher CRUD on subjects, CSV student enrollment, student credential receipt (shared via fixture), student submission cycle (fail → resubmit → pass), analytics page rendering, notification preferences toggle, feedback flow, quiz flow

**Non-Goals:**
- 100% branch coverage at the browser level (leave edge-case branching to unit tests)
- CI integration configuration (deferred; tests are long-running)
- Visual/snapshot regression testing
- API-only tests (those live in `tests/integration/`)

## Decisions

### 1. Playwright over Selenium

Playwright has async-native Python bindings, built-in auto-wait (eliminates most `sleep` calls), superior network interception, and first-class headless Chromium support. Selenium requires an external driver binary and has flakier timing. **Decision: Playwright.**

### 2. pytest-bdd for Gherkin parsing

`pytest-bdd` integrates cleanly with the existing pytest setup, allowing shared fixtures, parametrize, and normal pytest reporting. Alternatives (behave, radish) lack tight pytest integration or Playwright bindings. **Decision: pytest-bdd.**

### 3. Isolated Docker Compose stack for E2E

A separate `docker-compose.e2e.yml` spins up Postgres on port 5433 and the app on port 8001 so E2E tests never interfere with the dev environment. The stack is started/stopped by a `make e2e` target. Alternatively, tests could use the dev stack, but that risks data pollution and port conflicts. **Decision: separate compose stack.**

### 4. Teacher seeded via direct DB insert, not UI

The teacher account is the root of all test flows; creating it via UI would require admin flows not yet in scope. A `psycopg2` direct insert in a pytest session-scoped fixture mirrors the documented "manual DB insert" step. Student accounts are created by the teacher via CSV upload — so those go through the full UI path. **Decision: DB-seed teacher, UI-create students.**

### 5. State sharing between BDD steps via pytest fixtures

pytest-bdd step functions receive shared state through a mutable `context` dict fixture (session-scoped for the teacher account, function-scoped for per-scenario state like student credentials). This is the idiomatic pytest-bdd pattern for inter-step communication without globals. **Decision: context dict fixture.**

### 6. Feature file organisation

One `.feature` file per major user flow:
- `teacher_auth.feature`
- `subject_management.feature`
- `student_enrollment.feature`
- `student_submission.feature`
- `analytics.feature`
- `notifications.feature`
- `feedback.feature`
- `quiz.feature`

Steps are split into per-domain `steps/` modules so adding a new scenario only touches one file. **Decision: one feature per domain, step modules mirroring feature names.**

## Risks / Trade-offs

- [Flakiness from network timing] → Playwright auto-wait + explicit `expect` assertions reduce this; page-object helpers abstract waits centrally
- [Long test runtime (>5 min)] → Acceptable per requirements; suite is meant for accuracy not speed
- [E2E DB migration diverging from dev DB] → `docker-compose.e2e.yml` runs the same alembic migrations the app runs at startup, so schema is always in sync
- [Subject ZIP config must match expected plugin] → Feature file documents the exact ZIP structure; a sample fixture ZIP is committed to `tests/e2e/fixtures/`

## Migration Plan

1. Add `playwright`, `pytest-bdd`, `pytest-playwright` to `pyproject.toml` dev dependencies
2. Create `docker-compose.e2e.yml`
3. Scaffold `tests/e2e/` directory with `conftest.py`, `features/`, `steps/`, `fixtures/`
4. Write feature files and step implementations
5. Add `make e2e` target
6. Update `README.md` with "Running E2E tests" section

Rollback: the E2E suite is purely additive — removing `tests/e2e/` and reverting `pyproject.toml` fully undoes it.

## Open Questions

- Should the app container in E2E compose mount source live (for fast iteration) or use a pre-built image? → Live mount preferred during development.
- Should `make e2e` block until all tests pass or stream results? → Stream and return exit code so the user can Ctrl-C.
