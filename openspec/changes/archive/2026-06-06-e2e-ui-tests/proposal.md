## Why

The submissions-checker app has grown to cover teacher/student portals, assignment workflows, analytics, notifications, feedback, and quiz flows — but has no browser-level test coverage to confirm these features work end-to-end as real users experience them. E2E tests will catch UI regressions, broken flows, and integration failures that unit and API tests miss.

## What Changes

- Add a Playwright-based E2E test suite under `tests/e2e/`
- Tests written in Gherkin (BDD) using `pytest-bdd` with Playwright for browser automation
- A dedicated test Docker Compose stack (`docker-compose.e2e.yml`) with an isolated Postgres DB, app, and localstack — never touches the dev/prod database
- A `Makefile` target and `README` section documenting how to run with options (headed/headless, single scenario, tags)
- Shared test state (teacher credentials, student credentials, subject config) passed between BDD steps via fixtures
- Coverage: teacher login, subject creation via ZIP config, student enrollment via CSV, student login, assignment submission (fail → pass cycle), analytics dashboard, notification preferences, feedback flow, quiz flow

## Capabilities

### New Capabilities
- `e2e-test-suite`: Full browser E2E test suite covering teacher and student portal workflows using Playwright + pytest-bdd in Gherkin BDD syntax

### Modified Capabilities
<!-- none -->

## Impact

- New dev-dependency: `playwright`, `pytest-bdd`, `pytest-playwright`
- New file tree: `tests/e2e/` (features, steps, fixtures, conftest)
- New `docker-compose.e2e.yml` with isolated DB on port 5433
- `pyproject.toml` updated with e2e test group
- No production code changes; tests call the live app over HTTP via Playwright
