# Tests

Four layers, each runnable on its own. All but the unit layer use
[testcontainers](https://testcontainers.com/) or a Dockerised stack, so **Docker
must be running** for everything except `tests/unit`.

| Layer | Path | What it covers | Needs |
|-------|------|----------------|-------|
| **Unit** | `tests/unit/` | Pure logic in isolation: security (bcrypt/JWT), state machine, safe-zip extraction, config validation, similarity, notification templates, the AI provider client (SDK mocked). | nothing (fast) |
| **Integration** | `tests/integration/` | DB models, the outbox processor, scheduled workers, the config-apply / plugin-loader pipeline, and worker task dispatch against a real Postgres (+ Redis) container. | Docker |
| **Functional** | `tests/functional/` | The real FastAPI app driven over ASGI against a Postgres container with the **full authentication/authorization stack active**. Primary coverage for permissions and security. | Docker |
| **E2E / BDD** | `tests/e2e/` | `pytest-bdd` scenarios driving a real browser (Playwright) against the Dockerised app stack. | Docker + Playwright, run via its own config |

## Setup

```bash
make install          # installs the project + [dev] extra (incl. pytest, testcontainers, redis client)
```

For the e2e layer also install the `e2e` extra and Playwright browsers:

```bash
uv pip install -e ".[dev,e2e]"
playwright install chromium
```

## Running

```bash
make test              # unit + integration + functional, with coverage (excludes e2e)
make test-unit         # fast, no Docker
make test-integration  # needs Docker
make test-functional   # needs Docker
make e2e               # spins up the e2e Docker stack, runs the BDD suite, tears it down
```

Run a single file or test:

```bash
pytest tests/functional/test_auth_security.py -v
pytest tests/functional/test_teacher_portal.py -k cross_teacher
```

The default `pytest` run (configured in `pyproject.toml`) targets `tests/`,
excludes `tests/e2e` (it has its own `pytest-e2e.ini`, needs the live stack, and
declares `pytest_plugins`), and emits coverage to the terminal + `htmlcov/`.

## How the functional harness works (`tests/functional/conftest.py`)

- One Postgres container per session; the **full schema is created once** via a
  synchronous engine. The async engine is **function-scoped** so it lives on each
  test's own event loop (a session-scoped async engine triggers asyncpg
  "attached to a different loop" errors under pytest-asyncio).
- The app's `get_db` dependency is overridden to the test database via
  `app.dependency_overrides`; the rest of the stack (auth cookies, JWT decode,
  role guards, object-level authz) runs for real.
- Every table is `TRUNCATE … RESTART IDENTITY` before each test, so tests are
  isolated and ids are deterministic.
- Fixtures: `client`, `teacher_client` / `student_client` / `admin_client`
  (pre-authenticated), `db` (a session for arranging/asserting state),
  `authenticate(client, user)`, and factories `make_user` / `make_student` /
  `make_group`.

Coverage of async DB handlers requires `[tool.coverage.run] concurrency =
["greenlet", "thread"]` (set in `pyproject.toml`) — without it, coverage.py drops
its trace function after the first awaited DB call and under-reports every
DB-touching handler.
