# Teacher & Student Features — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining audit items: password change, login throttling + CSRF origin check, deadline reminders, grade export / subject delete in the UI, semester admin, known bugs #1 and #16, teacher unstick controls, bulk actions, a cross-student similarity report, and the docs for all of it.

**Architecture:** Additive. Two small `core/` modules (`rate_limit.py`, `csrf_middleware.py`), one scheduled job (`workers/scheduled/deadline_reminders.py`), one migration (`0028` drops the all-versions hash constraint), a handful of routes in the existing routers, one new admin page, one new teacher page (similarity), and a shared `_apply_review_decision` helper so single and bulk review share one code path. Every state change still goes through `state_machine.transition`.

**Tech Stack:** FastAPI/Starlette middleware, SQLAlchemy async, APScheduler, Alembic, pytest functional layer (auth on, Postgres testcontainer), ruff/mypy.

**Spec:** `docs/feature_audit.md` C1, C11, B3, B5, B7, B8 (#1, #16), B10, C7, C8, C4 (doc-only — approval already sends a configured quiz); B9 explicitly excluded by the user.

## Global Constraints

- `uv run --frozen …`; gates (`ruff check`, `ruff format --check`, `mypy src/`, `pytest -q` = 1011 tests at start) green after every task.
- New UI strings in `i18n/uk.yml`, referenced via `vocab.<section>.<key>`; no unreferenced keys.
- Status changes only via `state_machine.transition`; new events are added to `_TRANSITIONS` and to `tests/unit/test_state_machine.py::LEGAL`.
- Side effects that must survive a crash go through the outbox in the same transaction.
- Teacher routes: `TeacherUser` dependency + `require_subject_access` (owner or ADMIN) for subject-scoped resources; audited mutations use `services.audit.audit(db, action=…, actor_id=…, actor_username=…, **details)`.
- Commit format as before (imperative subject, why-body, `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`).

---

### Task 1: Change password for a logged-in user (C1)

**Files:**
- Modify: `src/submissions_checker/api/routes/auth.py` (two routes)
- Create: `templates/change_password.html`
- Modify: `templates/base.html` (nav link for every role, next to the settings link), `i18n/uk.yml` (`auth:` + `nav:` keys)
- Test: `tests/functional/test_auth_flows.py`

**Interfaces:**
- Produces: `GET /auth/change-password` (renders form), `POST /auth/change-password` with `current_password, new_password, confirm_password` → 303 to `/auth/change-password?changed=1` on success; 422 on validation failure; 401 when anonymous (via `CurrentUser`). Audit action `change_password`.

- [ ] **Step 1: Failing tests**

Append to `tests/functional/test_auth_flows.py`:
```python
# ── Change password (logged-in) ───────────────────────────────────────────────


async def test_change_password_requires_login(client: AsyncClient) -> None:
    assert (await client.get("/auth/change-password")).status_code == 401
    resp = await client.post(
        "/auth/change-password",
        data={"current_password": "x", "new_password": "y" * 8, "confirm_password": "y" * 8},
    )
    assert resp.status_code == 401


async def test_change_password_updates_hash_and_old_password_stops_working(
    client: AsyncClient, make_user, login, db
) -> None:
    user = await make_user(role=UserRole.STUDENT, username="carol", password=PASSWORD)
    login(client, user)
    resp = await client.post(
        "/auth/change-password",
        data={
            "current_password": PASSWORD,
            "new_password": "N3wPassw0rd!",
            "confirm_password": "N3wPassw0rd!",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    await db.refresh(user)
    assert bcrypt.checkpw(b"N3wPassw0rd!", user.password_hash.encode())
    assert not bcrypt.checkpw(PASSWORD.encode(), user.password_hash.encode())


@pytest.mark.parametrize(
    "current,new,confirm",
    [
        ("wrong-current", "N3wPassw0rd!", "N3wPassw0rd!"),
        (PASSWORD, "short", "short"),
        (PASSWORD, "N3wPassw0rd!", "different!!"),
        (PASSWORD, PASSWORD, PASSWORD),
    ],
)
async def test_change_password_rejects_bad_input_with_422(
    client: AsyncClient, make_user, login, db, current: str, new: str, confirm: str
) -> None:
    user = await make_user(role=UserRole.TEACHER, username="dave", password=PASSWORD)
    login(client, user)
    resp = await client.post(
        "/auth/change-password",
        data={"current_password": current, "new_password": new, "confirm_password": confirm},
    )
    assert resp.status_code == 422
    await db.refresh(user)
    assert bcrypt.checkpw(PASSWORD.encode(), user.password_hash.encode())
```
(`login` fixture: `tests/functional/conftest.py:149`, signature `login(client, user)`.) Run → FAIL (404/405).

- [ ] **Step 2: Routes**

In `auth.py` add imports `from submissions_checker.api.dependencies import CurrentUser, DBSession` (replace the existing `DBSession` import) and `from submissions_checker.services.audit import audit`, then:
```python
_MIN_PASSWORD_LEN = 8


def _render_change_password(
    request: Request, current_user: CurrentUser, *, error: str | None, changed: bool, status_code: int = 200
) -> HTMLResponse:
    return render(
        request,
        "change_password.html",
        {"current_user": current_user, "error": error, "changed": changed},
        status_code=status_code,
    )


@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(
    request: Request, current_user: CurrentUser, changed: int = 0
) -> HTMLResponse:
    return _render_change_password(request, current_user, error=None, changed=bool(changed))


@router.post("/change-password", response_model=None)
async def change_password(
    request: Request,
    db: DBSession,
    current_user: CurrentUser,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
) -> HTMLResponse | RedirectResponse:
    user = await db.get(User, current_user.user_id)
    if user is None:
        raise HTTPException(status_code=404)
    vocab = get_vocab(request.cookies.get("lang"))["auth"]
    error: str | None = None
    if not verify_password(current_password, user.password_hash):
        error = vocab["error_current_wrong"]
    elif new_password != confirm_password:
        error = vocab["error_mismatch"]
    elif len(new_password) < _MIN_PASSWORD_LEN:
        error = vocab["error_too_short"]
    elif new_password == current_password:
        error = vocab["error_same_as_current"]
    if error is not None:
        return _render_change_password(request, current_user, error=error, changed=False, status_code=422)

    user.password_hash = hash_password(new_password)
    await audit(
        db,
        action="change_password",
        actor_id=current_user.user_id,
        actor_username=current_user.username,
    )
    await db.commit()
    return RedirectResponse(url="/auth/change-password?changed=1", status_code=status.HTTP_303_SEE_OTHER)
```
Add `from submissions_checker.core.i18n import get_vocab`. (Existing reset-password handlers hard-code English error strings; the new one reads vocab so it matches the Ukrainian UI.)

- [ ] **Step 3: Template + vocab + nav**

`templates/change_password.html`:
```html
{% extends "base.html" %}
{% block title %}{{ vocab.auth.change_page_heading }}{% endblock %}
{% block content %}
<div class="max-w-md mx-auto">
  <h1 class="text-2xl font-bold text-slate-900 mb-1">{{ vocab.auth.change_page_heading }}</h1>
  <p class="text-slate-500 text-sm mb-6">{{ vocab.auth.change_page_subtitle }}</p>
  {% if changed %}
  <div class="mb-4 px-4 py-3 rounded-lg bg-green-50 border border-green-200 text-green-800 text-sm">{{ vocab.auth.password_changed_logged_in }}</div>
  {% endif %}
  {% if error %}
  <div class="mb-4 px-4 py-3 rounded-lg bg-red-50 border border-red-200 text-red-700 text-sm">{{ error }}</div>
  {% endif %}
  <form method="POST" action="/auth/change-password" class="bg-white rounded-xl border border-slate-200 shadow-sm p-5 space-y-4">
    <div>
      <label class="block text-sm font-medium text-slate-700 mb-1" for="current_password">{{ vocab.auth.current_password }}</label>
      <input id="current_password" name="current_password" type="password" required autocomplete="current-password"
             class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"/>
    </div>
    <div>
      <label class="block text-sm font-medium text-slate-700 mb-1" for="new_password">{{ vocab.auth.new_password }}</label>
      <input id="new_password" name="new_password" type="password" required minlength="8" autocomplete="new-password"
             class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"/>
    </div>
    <div>
      <label class="block text-sm font-medium text-slate-700 mb-1" for="confirm_password">{{ vocab.auth.confirm_password }}</label>
      <input id="confirm_password" name="confirm_password" type="password" required minlength="8" autocomplete="new-password"
             class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"/>
    </div>
    <button type="submit" class="w-full bg-indigo-600 hover:bg-indigo-700 text-white font-medium text-sm px-4 py-2.5 rounded-lg transition-colors">{{ vocab.auth.set_new_password }}</button>
  </form>
</div>
{% endblock %}
```
`i18n/uk.yml` under `auth:`:
```yaml
  change_page_heading: Змінити пароль
  change_page_subtitle: Введіть поточний пароль і оберіть новий (щонайменше 8 символів).
  current_password: Поточний пароль
  password_changed_logged_in: Пароль змінено.
  error_current_wrong: Поточний пароль неправильний.
  error_mismatch: Паролі не збігаються.
  error_too_short: Пароль має містити щонайменше 8 символів.
  error_same_as_current: Новий пароль має відрізнятися від поточного.
```
under `nav:`: `  change_password_link: Пароль`.
`templates/base.html`: right before the `{% if current_user.role == "STUDENT" %}` settings block add
```html
          <a href="/auth/change-password"
             class="text-xs font-medium text-indigo-200 hover:text-white transition-colors hidden sm:block">
            {{ vocab.nav.change_password_link }}
          </a>
```
(inside the `{% if current_user %}` region the other links live in — check the surrounding `{% if %}`).

- [ ] **Step 4: Verify + commit**

```bash
uv run --frozen --extra dev pytest tests/functional/test_auth_flows.py tests/functional/test_auth_security.py -q -o addopts=""
uv run --frozen ruff check src/ tests/ && uv run --frozen mypy src/
git add -A && git commit -m "Let a logged-in user change their own password

Students receive generated credentials by email and had no way to pick
their own password short of the forgot-password email round trip.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Login and forgot-password throttling (C11a)

**Files:**
- Create: `src/submissions_checker/core/rate_limit.py`
- Modify: `src/submissions_checker/core/config.py` (`login_max_attempts: int = 10`, `login_window_seconds: int = 900`)
- Modify: `src/submissions_checker/api/routes/auth.py` (login + forgot-password)
- Modify: `i18n/uk.yml` (`auth.error_too_many_attempts`)
- Test: `tests/unit/test_rate_limit.py`, `tests/functional/test_auth_flows.py`

**Interfaces:**
- Produces:
  ```python
  class SlidingWindowLimiter:
      def __init__(self, max_attempts: int, window_seconds: int, *, clock: Callable[[], float] = time.monotonic) -> None
      def is_blocked(self, key: str) -> bool
      def record_failure(self, key: str) -> None
      def reset(self, key: str) -> None
  login_limiter: SlidingWindowLimiter   # module-level, built from settings on first use via get_login_limiter()
  def client_ip(request: Request) -> str  # X-Forwarded-For first hop (Caddy sets it) else request.client.host
  ```
  Login: key `f"{ip}|{username.lower()}"`; forgot-password: key `f"forgot|{ip}"`. Blocked → 429 rendering the same page with `error_too_many_attempts`. Process-local by design (documented).

- [ ] **Step 1: Failing unit test**

`tests/unit/test_rate_limit.py`:
```python
from submissions_checker.core.rate_limit import SlidingWindowLimiter


def test_blocks_after_max_failures_within_window() -> None:
    now = [1000.0]
    lim = SlidingWindowLimiter(3, 60, clock=lambda: now[0])
    for _ in range(3):
        assert lim.is_blocked("k") is False
        lim.record_failure("k")
    assert lim.is_blocked("k") is True
    now[0] += 61
    assert lim.is_blocked("k") is False


def test_reset_clears_failures() -> None:
    lim = SlidingWindowLimiter(1, 60)
    lim.record_failure("k")
    assert lim.is_blocked("k")
    lim.reset("k")
    assert not lim.is_blocked("k")


def test_keys_are_independent() -> None:
    lim = SlidingWindowLimiter(1, 60)
    lim.record_failure("a")
    assert lim.is_blocked("a") and not lim.is_blocked("b")
```

- [ ] **Step 2: Failing functional test**

```python
async def test_login_is_throttled_after_repeated_failures(client: AsyncClient, make_user) -> None:
    from submissions_checker.core import rate_limit

    rate_limit.get_login_limiter().reset("testclient|erin")
    await make_user(role=UserRole.TEACHER, username="erin", password=PASSWORD)
    for _ in range(10):
        r = await client.post("/auth/login", data={"username": "erin", "password": "nope"})
        assert r.status_code == 401
    r = await client.post("/auth/login", data={"username": "erin", "password": "nope"})
    assert r.status_code == 429
    # Even the right password is refused while blocked.
    r = await client.post("/auth/login", data={"username": "erin", "password": PASSWORD})
    assert r.status_code == 429
```
(`client.host` under httpx ASGITransport is `"testclient"`; adjust the key if the implementation reads it differently.)

- [ ] **Step 3: Implement**

`core/rate_limit.py`:
```python
"""In-memory sliding-window throttle for credential endpoints.

Process-local on purpose: the production stack runs two replicas, so an attacker
gets at most 2× the configured budget — still a hard stop on online guessing, with
no Redis to run. Successful logins reset their key.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

from fastapi import Request

from submissions_checker.core.config import get_settings


class SlidingWindowLimiter:
    def __init__(
        self, max_attempts: int, window_seconds: int, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._max = max_attempts
        self._window = window_seconds
        self._clock = clock
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._events.setdefault(key, deque())
        cutoff = now - self._window
        while q and q[0] <= cutoff:
            q.popleft()
        if not q:
            self._events.pop(key, None)
            q = self._events.setdefault(key, deque())
        return q

    def is_blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key, self._clock())) >= self._max

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._prune(key, self._clock()).append(self._clock())

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


_login_limiter: SlidingWindowLimiter | None = None


def get_login_limiter() -> SlidingWindowLimiter:
    global _login_limiter
    if _login_limiter is None:
        s = get_settings()
        _login_limiter = SlidingWindowLimiter(s.login_max_attempts, s.login_window_seconds)
    return _login_limiter


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
```
`config.py` (after `secret_key`/`debug` block):
```python
    # Credential-endpoint throttling (per process, per client IP + username).
    login_max_attempts: int = 10
    login_window_seconds: int = 900
```
`auth.py` login: before the DB lookup
```python
    limiter = get_login_limiter()
    key = f"{client_ip(request)}|{username.strip().lower()}"
    if limiter.is_blocked(key):
        return render(request, "login.html", {"current_user": None, "error": vocab_auth(request)["error_too_many_attempts"]}, status_code=429)
```
on failure `limiter.record_failure(key)` before returning 401; on success `limiter.reset(key)`. Add a tiny helper `def vocab_auth(request: Request) -> dict[str, Any]: return get_vocab(request.cookies.get("lang")).get("auth", {})`. Forgot-password: key `f"forgot|{client_ip(request)}"`, `is_blocked` → 429 render, `record_failure` on every POST (each request costs budget; it sends mail).
`i18n/uk.yml` `auth:`: `  error_too_many_attempts: Забагато спроб. Спробуйте ще раз за 15 хвилин.`

- [ ] **Step 4: Verify + commit**

```bash
uv run --frozen --extra dev pytest tests/unit/test_rate_limit.py tests/functional/test_auth_flows.py -q -o addopts=""
uv run --frozen ruff check src/ tests/ && uv run --frozen mypy src/
git add -A && git commit -m "Throttle login and forgot-password per client and username

Online password guessing had no limit at all. A process-local sliding
window (10 failures / 15 min by default) returns 429; a successful login
clears the key. Two replicas double the budget, which is documented.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: CSRF origin check middleware (C11b)

**Files:**
- Create: `src/submissions_checker/core/csrf_middleware.py`
- Modify: `src/submissions_checker/main.py` (add middleware after CORS)
- Test: `tests/functional/test_auth_security.py`

**Interfaces:**
- Produces: `OriginCheckMiddleware(app)`. For `POST/PUT/PATCH/DELETE`: if `Sec-Fetch-Site` is present and not `same-origin`/`none` → 403 `{"detail": "cross-site request refused"}`; else if `Origin` present and its host (host[:port]) ≠ request `Host` → 403; otherwise pass. GET/HEAD/OPTIONS never checked. No exemptions (there is no API surface for third parties).

- [ ] **Step 1: Failing tests**

Append to `tests/functional/test_auth_security.py`:
```python
async def test_cross_site_post_is_refused_by_origin(client: AsyncClient) -> None:
    r = await client.post(
        "/auth/login",
        data={"username": "x", "password": "y"},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403


async def test_cross_site_post_is_refused_by_fetch_metadata(client: AsyncClient) -> None:
    r = await client.post(
        "/auth/login",
        data={"username": "x", "password": "y"},
        headers={"Sec-Fetch-Site": "cross-site", "Origin": "http://test"},
    )
    assert r.status_code == 403


async def test_same_origin_post_passes(client: AsyncClient) -> None:
    r = await client.post(
        "/auth/login",
        data={"username": "x", "password": "y"},
        headers={"Origin": "http://test", "Sec-Fetch-Site": "same-origin"},
    )
    assert r.status_code == 401  # reached the handler; bad credentials


async def test_get_is_never_origin_checked(client: AsyncClient) -> None:
    r = await client.get("/auth/login", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200
```
(The functional client's base URL is `http://test`, so `Host: test`.)

- [ ] **Step 2: Implement**

```python
"""Refuse state-changing requests that a browser marks as cross-site.

Session cookies are SameSite=Strict, which already blocks CSRF in every browser
that honours it. This is the second layer OWASP recommends: Fetch Metadata
(``Sec-Fetch-Site``) when present, ``Origin`` vs ``Host`` otherwise. Requests
that carry neither header (curl, tests, old clients) are allowed — a browser
always sends at least one of them on a cross-site POST.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def _header(scope: Scope, name: bytes) -> str | None:
    for k, v in scope.get("headers", []):
        if k == name:
            return v.decode("latin-1")
    return None


def is_cross_site(scope: Scope) -> bool:
    site = _header(scope, b"sec-fetch-site")
    if site is not None:
        return site not in ("same-origin", "none")
    origin = _header(scope, b"origin")
    if origin is None or origin == "null":
        return origin == "null"
    host = _header(scope, b"host") or ""
    return urlsplit(origin).netloc.lower() != host.lower()


class OriginCheckMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("method", "GET") in _UNSAFE and is_cross_site(scope):
            response = JSONResponse({"detail": "cross-site request refused"}, status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
```
`main.py`: `app.add_middleware(OriginCheckMiddleware)` right after the CORS middleware (before Prometheus so refused requests are still counted — Prometheus is added last and wraps outermost; order: CORS, OriginCheck, Prometheus as now).

- [ ] **Step 3: Verify + commit**

Run the whole functional suite (every POST test must still pass — none send `Origin`):
```bash
uv run --frozen --extra dev pytest tests/functional -q -o addopts="" && uv run --frozen ruff check src/ tests/ && uv run --frozen mypy src/
git add -A && git commit -m "Refuse cross-site state-changing requests

SameSite=Strict cookies were the only CSRF control. Fetch Metadata and an
Origin/Host comparison add the second layer without touching any of the
34 forms.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Deadline reminders actually fire (B3)

**Files:**
- Modify: `src/submissions_checker/db/models/enums.py` (`NotificationCase.DEADLINE_REMINDER`), `src/submissions_checker/api/routes/student_portal.py` (`_ALL_CASES`), `i18n/uk.yml` if the label is vocab-driven (it is a plain string in `_ALL_CASES`; keep the pattern)
- Modify: `src/submissions_checker/core/config.py` (`deadline_reminder_days_before: int = 2`, `deadline_reminder_interval: int = 3600`), `src/submissions_checker/core/scheduler.py` (register)
- Create: `src/submissions_checker/workers/scheduled/deadline_reminders.py`
- Modify: `src/submissions_checker/workers/tasks/notification_tasks.py::execute_deadline_reminder_task` (honour the preference)
- Test: `tests/integration/test_deadline_reminders.py` (new)

**Interfaces:**
- Produces: `async def enqueue_due_deadline_reminders(db: AsyncSession, *, now: datetime, days_before: int) -> int` (pure-ish, testable; returns rows enqueued) and `async def run_deadline_reminders() -> None` (locks 7937, opens a session, calls the former). Outbox payload: `{"student_id", "subjects_assignment_id", "deadline_str"}` — exactly what `execute_deadline_reminder_task` reads. Dedup: no existing `DEADLINE_REMINDER` outbox row whose payload has the same `student_id` + `subjects_assignment_id`.

- [ ] **Step 1: Failing integration tests**

`tests/integration/test_deadline_reminders.py` — copy the arrangement helpers style from `tests/integration/test_subject_stats_refresh.py` (`db_session`, `monkeypatch`, `_patch` that swaps `get_session`). Tests:
```python
async def test_enqueues_one_reminder_per_unsubmitted_student(db_session, monkeypatch) -> None:
    # subject with one assignment due in 1 day, two enrolled students, one already submitted
    ... build Group/Student×2/Subject/SubjectsStudents×2/SubjectsAssignment(deadline=now+1d)/StudentAssignment×2
    ... Submission for student A
    n = await enqueue_due_deadline_reminders(db_session, now=now, days_before=2)
    assert n == 1
    rows = (await db_session.execute(select(OutboxMessage).where(OutboxMessage.event_type == OutboxEventType.DEADLINE_REMINDER))).scalars().all()
    assert len(rows) == 1 and rows[0].payload["student_id"] == student_b.id


async def test_is_idempotent(db_session, monkeypatch) -> None:
    ... same arrangement
    assert await enqueue_due_deadline_reminders(db_session, now=now, days_before=2) == 1
    assert await enqueue_due_deadline_reminders(db_session, now=now, days_before=2) == 0


async def test_skips_far_deadlines_past_deadlines_and_opted_out(db_session, monkeypatch) -> None:
    ... assignment due in 10 days → 0; due yesterday → 0; due tomorrow but NotificationPreference(case=DEADLINE_REMINDER, method=EMAIL, enabled=False) → 0


async def test_job_entry_point_runs_under_lock(db_session, monkeypatch) -> None:
    _patch(monkeypatch, db_session)  # get_session → db_session
    await run_deadline_reminders()   # must not raise on an empty DB
```
Write them fully (the `...` are for this plan's brevity; the executor writes real code using the models imported in that test module). Run → ImportError.

- [ ] **Step 2: Implement the job**

```python
"""Scheduled job: enqueue DEADLINE_REMINDER emails for unsubmitted work.

The email task, template and outbox branch existed since the notification
system shipped; nothing ever produced the event. This walks every active
assignment whose deadline falls within the next `deadline_reminder_days_before`
days and enqueues one reminder per enrolled real student who has no
submission yet and has not opted out. Dedup is by the outbox itself: a
FINISHED row for the same (student, assignment) means it was sent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, exists, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core.config import get_settings
from submissions_checker.core.logging import get_logger
from submissions_checker.db.models import (
    NotificationPreference, OutboxMessage, Student, StudentAssignment, Subject,
    SubjectsAssignment, SubjectsStudents, Submission,
)
from submissions_checker.db.models.enums import (
    EntityType, NotificationCase, NotificationMethod, OutboxEventType, OutboxMessageState, SubjectStatus,
)
from submissions_checker.db.session import get_session

logger = get_logger(__name__)
DEADLINE_REMINDERS_LOCK_ID = 7937


async def enqueue_due_deadline_reminders(
    db: AsyncSession, *, now: datetime, days_before: int
) -> int:
    horizon = now + timedelta(days=days_before)
    due = await db.execute(
        select(SubjectsAssignment)
        .join(Subject, Subject.id == SubjectsAssignment.subject_id)
        .where(
            Subject.status == SubjectStatus.ACTIVE,
            SubjectsAssignment.deadline.is_not(None),
            SubjectsAssignment.deadline > now,
            SubjectsAssignment.deadline <= horizon,
        )
    )
    enqueued = 0
    for sa in due.scalars():
        has_submission = exists().where(
            and_(
                Submission.students_assignment_id == StudentAssignment.id,
            )
        )
        already_sent = exists().where(
            and_(
                OutboxMessage.event_type == OutboxEventType.DEADLINE_REMINDER,
                text("outbox_messages.payload->>'student_id' = CAST(students.id AS TEXT)"),
                text("outbox_messages.payload->>'subjects_assignment_id' = CAST(:sa_id AS TEXT)"),
            )
        ).params(sa_id=sa.id)
        opted_out = exists().where(
            and_(
                NotificationPreference.student_id == Student.id,
                NotificationPreference.case == NotificationCase.DEADLINE_REMINDER,
                NotificationPreference.method == NotificationMethod.EMAIL,
                NotificationPreference.enabled.is_(False),
            )
        )
        students = await db.execute(
            select(Student.id)
            .join(SubjectsStudents, SubjectsStudents.student_id == Student.id)
            .outerjoin(
                StudentAssignment,
                and_(
                    StudentAssignment.student_id == Student.id,
                    StudentAssignment.subjects_assignment_id == sa.id,
                ),
            )
            .where(
                SubjectsStudents.subject_id == sa.subject_id,
                Student.type == EntityType.REAL,
                ~has_submission,
                ~already_sent,
                ~opted_out,
            )
        )
        deadline_str = sa.deadline.strftime("%Y-%m-%d %H:%M") if sa.deadline else ""
        for (student_id,) in students:
            db.add(
                OutboxMessage(
                    event_type=OutboxEventType.DEADLINE_REMINDER,
                    state=OutboxMessageState.PENDING,
                    payload={
                        "student_id": student_id,
                        "subjects_assignment_id": sa.id,
                        "deadline_str": deadline_str,
                    },
                )
            )
            enqueued += 1
    await db.commit()
    return enqueued


async def run_deadline_reminders() -> None:
    settings = get_settings()
    try:
        async with get_session() as db:
            got = await db.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": DEADLINE_REMINDERS_LOCK_ID})
            if not got.scalar():
                return
            try:
                n = await enqueue_due_deadline_reminders(
                    db, now=datetime.now(UTC), days_before=settings.deadline_reminder_days_before
                )
                logger.info("deadline_reminders_enqueued", count=n)
            finally:
                await db.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": DEADLINE_REMINDERS_LOCK_ID})
    except Exception as exc:  # noqa: BLE001 — same top-level catch as the other jobs
        logger.error("deadline_reminders_error", error=str(exc))
```
Note: `has_submission` must correlate on `StudentAssignment.id` from the outer join — with `StudentAssignment` outer-joined in the main query, `exists().where(Submission.students_assignment_id == StudentAssignment.id)` correlates automatically. Verify with the tests; if SQLAlchemy complains about correlation, add `.correlate(StudentAssignment)`.

Scheduler: register `run_deadline_reminders` with `IntervalTrigger(seconds=settings.deadline_reminder_interval)`, id `deadline_reminders`. Enum: `DEADLINE_REMINDER = "DEADLINE_REMINDER"` in `NotificationCase`; `_ALL_CASES` gets `(NotificationCase.DEADLINE_REMINDER, "Deadline Reminder")`. In `execute_deadline_reminder_task`, after loading the student: `if not await _is_email_enabled(db, student.id, NotificationCase.DEADLINE_REMINDER): return`.

- [ ] **Step 3: Verify + commit**

```bash
uv run --frozen --extra dev pytest tests/integration/test_deadline_reminders.py tests/integration/test_notification_tasks_extra.py tests/functional/test_student_portal.py tests/unit/test_scheduler*.py -q -o addopts=""
uv run --frozen ruff check src/ tests/ && uv run --frozen mypy src/
git add -A && git commit -m "Enqueue deadline reminders from a scheduled job

The DEADLINE_REMINDER email existed end to end except for a producer.
A job now scans assignments due within N days and enqueues one reminder
per unsubmitted, opted-in student, deduplicated through the outbox.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Grade export and subject delete in the Операції tab (B5)

**Files:**
- Modify: `templates/teacher_subject.html` (operations tab: new "Дані та життєвий цикл" card at the end of the tab), `i18n/uk.yml`
- Modify: `docs/feature_catalog.md` (§2 delete row and §4 export row: drop "endpoint only")
- Test: `tests/functional/test_teacher_portal.py`

- [ ] **Step 1: Failing test**

```python
async def test_operations_tab_offers_export_and_delete(client: AsyncClient, db, teacher) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    authenticate(client, teacher)
    resp = await client.get(f"/teacher/subjects/{subject.id}?tab=operations")
    assert resp.status_code == 200
    assert f"/teacher/subjects/{subject.id}/export.csv" in resp.text
    assert f'action="/teacher/subjects/{subject.id}/delete"' in resp.text
```
(Check how the route selects `default_tab` — a query param or a fragment; adapt the URL. If the tab is client-side only, the markup is in the page regardless.)

- [ ] **Step 2: Template + vocab**

Append inside `#tab-panel-operations`, after the feedback card:
```html
  <div class="bg-white rounded-xl border border-slate-200 shadow-sm p-5 mb-6">
    <h2 class="font-semibold text-slate-900 mb-1">{{ vocab.teacher.data_title }}</h2>
    <p class="text-sm text-slate-500 mb-4">{{ vocab.teacher.data_hint }}</p>
    <a href="/teacher/subjects/{{ subject.id }}/export.csv"
       class="inline-flex items-center gap-2 px-4 py-2 rounded-lg border border-slate-300 bg-slate-50 text-sm font-medium text-slate-700 hover:bg-slate-100 transition-colors">
      {{ vocab.teacher.export_grades_csv }}
    </a>
  </div>

  <div class="bg-white rounded-xl border border-red-200 shadow-sm p-5">
    <h2 class="font-semibold text-red-700 mb-1">{{ vocab.teacher.delete_subject_title }}</h2>
    <p class="text-sm text-slate-500 mb-4">{{ vocab.teacher.delete_subject_hint }}</p>
    <form method="POST" action="/teacher/subjects/{{ subject.id }}/delete" class="flex flex-col sm:flex-row sm:items-center gap-3">
      <label class="inline-flex items-center gap-2 text-sm text-slate-700">
        <input type="checkbox" required class="rounded border-slate-300">
        {{ vocab.teacher.delete_subject_confirm }}
      </label>
      <button type="submit" class="inline-flex items-center px-4 py-2 rounded-lg bg-red-600 text-white text-sm font-medium hover:bg-red-700 transition-colors">
        {{ vocab.teacher.delete_subject_button }}
      </button>
    </form>
  </div>
```
Vocab (`teacher:`): `data_title: Дані`, `data_hint: Вивантажити всі оцінки предмета у CSV.`, `export_grades_csv: Експорт оцінок (CSV)`, `delete_subject_title: Видалити предмет`, `delete_subject_hint: Предмет буде приховано для всіх; дані зберігаються і можуть бути відновлені адміністратором через БД.`, `delete_subject_confirm: Я розумію, що предмет зникне з усіх списків`, `delete_subject_button: Видалити`.

- [ ] **Step 3: Docs + commit**

`docs/feature_catalog.md`: remove "— **endpoint only, no UI button**" from both rows; in the §2 note replace "no subject delete, … no grade export button" wording with "no subject edit, no create/edit assignment, no quiz editor". `docs/teacher_journey_guide.md` §7 Exporting grades: mention the Операції tab button.
```bash
uv run --frozen --extra dev pytest tests/functional/test_teacher_portal.py -q -o addopts=""
git add -A && git commit -m "Offer grade export and subject delete on the Операції tab

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Semester administration (B7)

**Files:**
- Modify: `src/submissions_checker/api/routes/admin.py` (3 routes), `templates/admin_dashboard.html` (link), `i18n/uk.yml` (`admin:` keys)
- Create: `templates/admin_semesters.html`
- Test: `tests/functional/test_admin.py`
- Docs: `docs/admin_journey_guide.md` (new §6 "Semesters"), `docs/feature_catalog.md` §8

**Interfaces:**
- Produces: `GET /admin/semesters` (list, newest first, with an add form), `POST /admin/semesters` (`name, season[SPRING|FALL], start_date, end_date`; 422 on `end<=start` or overlap with an existing semester), `POST /admin/semesters/{id}` (same fields, update). Audit `create_semester` / `update_semester`.

- [ ] **Step 1: Failing tests**

```python
async def test_admin_creates_semester(admin_client: AsyncClient, db) -> None:
    r = await admin_client.post(
        "/admin/semesters",
        data={"name": "Summer 2036", "season": "SPRING", "start_date": "2036-07-01", "end_date": "2036-08-31"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    row = (await db.execute(select(Semester).where(Semester.name == "Summer 2036"))).scalar_one()
    assert row.season == "SPRING"


async def test_admin_semester_rejects_inverted_and_overlapping_dates(admin_client: AsyncClient, db) -> None:
    r = await admin_client.post("/admin/semesters", data={"name": "X", "season": "FALL", "start_date": "2036-09-01", "end_date": "2036-08-01"})
    assert r.status_code == 422
    r = await admin_client.post("/admin/semesters", data={"name": "Overlap", "season": "FALL", "start_date": "2026-10-01", "end_date": "2026-11-01"})
    assert r.status_code == 422  # Fall 2026 is seeded 2026-09-01..2027-01-31


async def test_admin_updates_semester(admin_client: AsyncClient, db) -> None:
    sem = (await db.execute(select(Semester).where(Semester.name == "Fall 2026"))).scalar_one()
    r = await admin_client.post(f"/admin/semesters/{sem.id}", data={"name": "Fall 2026", "season": "FALL", "start_date": "2026-09-01", "end_date": "2027-02-15"}, follow_redirects=False)
    assert r.status_code == 303
    await db.refresh(sem)
    assert str(sem.end_date) == "2027-02-15"


async def test_teacher_cannot_manage_semesters(teacher_client: AsyncClient) -> None:
    assert (await teacher_client.get("/admin/semesters")).status_code == 403
```
Also extend the existing parametrized `test_admin_pages_render_for_admin` path list with `"/admin/semesters"`. (The functional DB is created from migrations? Check `_schema_ready` in the functional conftest: if it uses `metadata.create_all`, seeded semesters do NOT exist — then create a `Semester` row in the test before asserting overlap/update.)

- [ ] **Step 2: Routes**

```python
_SEASONS = ("SPRING", "FALL")


def _parse_semester_form(name: str, season: str, start_date: str, end_date: str) -> tuple[str, str, date, date] | str:
    name = name.strip()
    season = season.strip().upper()
    if not name or season not in _SEASONS:
        return "invalid"
    try:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except ValueError:
        return "invalid"
    if end <= start:
        return "inverted"
    return name, season, start, end


async def _overlaps(db: AsyncSession, start: date, end: date, *, exclude_id: int | None) -> bool:
    q = select(Semester.id).where(Semester.start_date <= end, Semester.end_date >= start)
    if exclude_id is not None:
        q = q.where(Semester.id != exclude_id)
    return (await db.execute(q)).first() is not None


async def _render_semesters(request, db, current_user, *, error: str | None, status_code: int = 200):
    rows = (await db.execute(select(Semester).order_by(Semester.start_date.desc()))).scalars().all()
    today = date.today()
    return render(request, "admin_semesters.html", {"current_user": current_user, "semesters": rows, "today": today, "error": error, "seasons": _SEASONS}, status_code=status_code)


@router.get("/semesters", response_class=HTMLResponse)
async def admin_semesters(request: Request, db: DBSession, current_user: AdminUser) -> HTMLResponse: ...

@router.post("/semesters", response_model=None)
async def admin_create_semester(request, db, current_user, name: str = Form(...), season: str = Form(...), start_date: str = Form(...), end_date: str = Form(...)):
    parsed = _parse_semester_form(name, season, start_date, end_date)
    if isinstance(parsed, str): return await _render_semesters(..., error=parsed, status_code=422)
    n, s, start, end = parsed
    if await _overlaps(db, start, end, exclude_id=None): return await _render_semesters(..., error="overlap", status_code=422)
    db.add(Semester(name=n, season=s, start_date=start, end_date=end))
    await audit(db, action="create_semester", actor_id=current_user.user_id, actor_username=current_user.username, name=n)
    await db.commit()
    return RedirectResponse("/admin/semesters", status_code=303)

@router.post("/semesters/{semester_id}", response_model=None)
async def admin_update_semester(...): same, on the fetched row (404 if missing), exclude_id=semester_id, audit "update_semester".
```
Template `admin_semesters.html`: breadcrumbs like `admin_users.html`; an add form (name, season select, two date inputs); a table with one inline edit form per row (same four inputs prefilled + "Зберегти"); "поточний" badge when `start_date <= today <= end_date`; `error` mapped to `vocab.admin.semester_error_<error>`. Vocab (`admin:`): `semesters_title: Семестри`, `semesters_link: Семестри`, `semester_name: Назва`, `semester_season: Сезон`, `semester_start: Початок`, `semester_end: Кінець`, `semester_add: Додати семестр`, `semester_save: Зберегти`, `semester_current: поточний`, `semester_error_invalid: Заповніть усі поля коректно.`, `semester_error_inverted: Дата кінця має бути пізніше за дату початку.`, `semester_error_overlap: Період перетинається з іншим семестром.`. Dashboard link next to `/admin/audit`.

- [ ] **Step 3: Docs + commit**

`docs/admin_journey_guide.md`: new section "## 6. Semesters" (what they gate — course feedback — and the overlap rule); renumber the following sections. `docs/feature_catalog.md` §8: rows for the three routes. Remove the "Semesters have no management surface" item from the audit's B7 by marking it ✅.
```bash
uv run --frozen --extra dev pytest tests/functional/test_admin.py -q -o addopts="" && uv run --frozen ruff check src/ tests/ && uv run --frozen mypy src/
git add -A && git commit -m "Add semester administration for admins

Course feedback is keyed by the current semester, and the only semesters
were the ones a migration seeded through 2035. Admins can now list, add
and edit them; overlapping periods are refused.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Known bugs #1 and #16 (B8)

**Files:**
- Modify: `src/submissions_checker/services/config_apply.py` (`_execute_plan` update branch: claim `owner_id` when NULL; `_check_duplicate`: compare against the latest version only)
- Create: `alembic/versions/0028_drop_config_hash_uniqueness.py`
- Modify: `src/submissions_checker/db/models/subject_plugin_config.py` (remove the unique constraint from `__table_args__`; keep an index on `(subject_id, version)` if one exists)
- Modify: `docs/known_bugs.md` (#1, #16 → ✅)
- Test: `tests/integration/test_config_apply.py`

- [ ] **Step 1: Failing tests**

```python
async def test_apply_claims_ownerless_subject(db_session, svc_and_zip_helpers...) -> None:
    # Subject row with owner_id=None and code "demo101" already exists
    ... apply as teacher 7 → subject.owner_id == 7


async def test_reapplying_an_older_config_is_accepted(...) -> None:
    ... apply zip A (v1), apply zip B (v2), apply zip A again → result.changed is True, latest version == 3, its content_hash == sha256(A)
```
(Model the arrangement on the file's existing `test_apply_creates_subject`-style tests; they build a `ConfigApplyService(storage=None, plugins_dir=tmp_path)` and call `.apply(zip_bytes, owner_id, db)`.)

- [ ] **Step 2: Implement**

`_execute_plan` else-branch:
```python
        else:
            subject_created = False
            if subject.owner_id is None:
                # A subject created before ownership existed (or by the removed plugin
                # autoloader) is claimed by the first teacher who re-applies its config.
                subject.owner_id = owner_id
            self._apply_subject_fields(plan, new_cfg, subject, url_map, subject_code)
```
`_check_duplicate`: replace the `SubjectPluginConfig.content_hash == sha256` query with "latest version's hash":
```python
        latest_hash = await db.scalar(
            select(SubjectPluginConfig.content_hash)
            .where(SubjectPluginConfig.subject_id == subject_id)
            .order_by(SubjectPluginConfig.version.desc())
            .limit(1)
        )
        if latest_hash != sha256:
            return None
```
and update its docstring (rolling back to an earlier archive now creates a new version). Migration `0028`:
```python
def upgrade() -> None:
    op.drop_constraint("uq_subject_plugin_configs_subject_hash", "subject_plugin_configs", type_="unique")

def downgrade() -> None:
    op.create_unique_constraint("uq_subject_plugin_configs_subject_hash", "subject_plugin_configs", ["subject_id", "content_hash"])
```
Model: drop the `UniqueConstraint(... name="uq_subject_plugin_configs_subject_hash")` entry. Run on the dev DB from the host as in Plan 1. `known_bugs.md`: #1 → ✅ with "Fixed 2026-09-17: re-applying the config claims ownership when it is NULL"; #16 → ✅ "dedup is against the latest version only; migration 0028".

- [ ] **Step 3: Verify + commit**

```bash
uv run --frozen --extra dev pytest tests/integration/test_config_apply.py tests/integration/test_config_apply_edges.py tests/functional/test_apply_config.py tests/unit/test_config_apply_helpers.py -q -o addopts=""
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/submissions_checker uv run --frozen alembic upgrade head
git add -A && git commit -m "Claim ownerless subjects on re-apply and allow config rollbacks (0028)

Known bugs #1 and #16: an owner_id=NULL subject could never be managed,
and re-uploading an earlier config ZIP was refused because dedup matched
every stored version. Dedup now compares the latest version only, which
needs the (subject_id, content_hash) uniqueness dropped.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Unstick controls for the teacher (B10)

**Files:**
- Modify: `src/submissions_checker/core/state_machine.py` (new events), `tests/unit/test_state_machine.py` (LEGAL rows)
- Modify: `src/submissions_checker/api/routes/teacher_portal.py` (three routes + `_load_submission_for_teacher` helper; board context `stuck` set)
- Modify: `templates/teacher_assignment.html` (row action buttons), `i18n/uk.yml`
- Test: `tests/functional/test_teacher_portal.py`

**Interfaces:**
- State machine additions: `AI_REVIEW_FAILED: {"ai_review_skip_to_teacher": AWAITING_TEACHER_REVIEW}`; `"requeue_checks": PENDING` from `VALIDATING, TESTING, AWAITING_AI_REVIEW, AI_REVIEWING, AI_REVIEW_FAILED, VALIDATION_FAILED, TEST_FAILED, FAILED, AWAITING_TEACHER_REVIEW`.
- Routes (all `TeacherUser` + subject access via the submission's subject, 303 back to the assignment board, audited):
  - `POST /teacher/submissions/{id}/rerun-checks` — `transition(sub, "requeue_checks")`, `plugin_config_id=None`, `test_results=None`, `ai_review=None`, enqueue `RUN_CHECKS`.
  - `POST /teacher/submissions/{id}/retry-ai-review` — only from `AI_REVIEW_FAILED`: enqueue `RUN_AI_REVIEW` with `next_step` derived from the assignment's `review_mode` (`…then_teacher` → `teacher`, `…then_quiz` → `quiz`, else `completed`); status stays `AI_REVIEW_FAILED` (the task's `_enter_reviewing` handles it).
  - `POST /teacher/submissions/{id}/send-to-teacher` — only from `AI_REVIEW_FAILED`: `transition(sub, "ai_review_skip_to_teacher")` + `enqueue_teacher_review_notification`.
- Board: `stuck_ids: set[int]` = submissions in `VALIDATING/TESTING/AWAITING_AI_REVIEW/AI_REVIEWING` older than 30 minutes; template shows "Перезапустити перевірку" for stuck + failed statuses, and the two AI buttons for `AI_REVIEW_FAILED`.

- [ ] **Step 1: Failing tests**

Unit: add the new LEGAL rows. Functional:
```python
async def test_rerun_checks_requeues_and_unpins_config(client, db, teacher, make_student) -> None:
    ... submission status TEST_FAILED, plugin_config_id set, test_results set
    authenticate(client, teacher)
    r = await client.post(f"/teacher/submissions/{sub.id}/rerun-checks", follow_redirects=False)
    assert r.status_code == 303
    await db.refresh(sub)
    assert sub.status == SubmissionStatus.PENDING and sub.plugin_config_id is None and sub.test_results is None
    outbox = (await db.execute(select(OutboxMessage).where(OutboxMessage.event_type == OutboxEventType.RUN_CHECKS))).scalars().all()
    assert outbox and outbox[-1].payload["submission_id"] == sub.id


async def test_send_failed_ai_review_to_teacher(client, db, teacher, make_student) -> None:
    ... status AI_REVIEW_FAILED → POST send-to-teacher → AWAITING_TEACHER_REVIEW


async def test_retry_ai_review_enqueues_with_derived_next_step(client, db, teacher, make_student) -> None:
    ... sa.config = {"review_mode": "tests_then_ai_then_teacher"}; status AI_REVIEW_FAILED
    → POST retry-ai-review → RUN_AI_REVIEW row with payload next_step == "teacher"


async def test_unstick_routes_refuse_wrong_status(client, db, teacher, make_student) -> None:
    ... status COMPLETED → rerun-checks 409; retry-ai-review 409; send-to-teacher 409


async def test_other_teacher_cannot_unstick(client, db, make_user, make_student) -> None:
    ... 403
```

- [ ] **Step 2: Implement**

Add a helper in `teacher_portal.py`:
```python
async def _load_submission_for_teacher(db: DBSession, submission_id: int, current_user: TeacherUser) -> Submission:
    result = await db.execute(
        select(Submission).where(Submission.id == submission_id).options(
            selectinload(Submission.students_assignment).selectinload(StudentAssignment.subjects_assignment).selectinload(SubjectsAssignment.subject)
        )
    )
    submission = result.scalar_one_or_none()
    if submission is None:
        raise HTTPException(status_code=404)
    subject = submission.students_assignment.subjects_assignment.subject
    if current_user.role != UserRole.ADMIN and subject.owner_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="Not authorized for this subject")
    return submission


def _board_url(submission: Submission) -> str:
    sa = submission.students_assignment.subjects_assignment
    return f"/teacher/subjects/{sa.subject_id}/assignments/{sa.id}"


def _next_step_for(review_mode: str) -> str:
    if review_mode.endswith("then_teacher") or review_mode.endswith("ai_teacher"):
        return "teacher"
    if review_mode.endswith("then_quiz"):
        return "quiz"
    return "completed"
```
Routes follow the interface above; wrong-status → `HTTPException(409, "Submission is not in a state that allows this action")` (check via `try: transition(...) except InvalidTransitionError → 409` for rerun; explicit status check for the AI ones). Import `InvalidTransitionError`, `enqueue_teacher_review_notification` (from `workers.tasks.notification_tasks`), `metrics` not needed.

Board context: after `rows` are built compute
```python
    stale_cutoff = datetime.now(UTC) - timedelta(minutes=30)
    _IN_FLIGHT = {SubmissionStatus.VALIDATING, SubmissionStatus.TESTING, SubmissionStatus.AWAITING_AI_REVIEW, SubmissionStatus.AI_REVIEWING}
    stuck_ids = {r["submission_id"] for r in rows if r["submission_id"] and r["submission_status"] in _IN_FLIGHT and r["submitted_at"] and r["submitted_at"] < stale_cutoff}
```
(`submitted_at` is `Submission.created_at`; good enough as "age"). Pass `stuck_ids`. Template, inside the status cell after the review link:
```html
          {% set st = row.submission_status %}
          {% if row.submission_id and (st in ["VALIDATION_FAILED", "TEST_FAILED", "FAILED", "AI_REVIEW_FAILED"] or row.submission_id in stuck_ids) %}
          <form method="POST" action="/teacher/submissions/{{ row.submission_id }}/rerun-checks" class="inline">
            <button type="submit" class="ml-1.5 text-xs text-slate-500 hover:text-indigo-700 underline underline-offset-2">{{ vocab.teacher.rerun_checks }}</button>
          </form>
          {% endif %}
          {% if st == "AI_REVIEW_FAILED" %}
          <form method="POST" action="/teacher/submissions/{{ row.submission_id }}/retry-ai-review" class="inline"><button type="submit" class="ml-1.5 text-xs text-slate-500 hover:text-indigo-700 underline underline-offset-2">{{ vocab.teacher.retry_ai_review }}</button></form>
          <form method="POST" action="/teacher/submissions/{{ row.submission_id }}/send-to-teacher" class="inline"><button type="submit" class="ml-1.5 text-xs text-slate-500 hover:text-indigo-700 underline underline-offset-2">{{ vocab.teacher.send_to_teacher }}</button></form>
          {% endif %}
```
Vocab (`teacher:`): `rerun_checks: Перезапустити перевірку`, `retry_ai_review: Повторити AI-рецензію`, `send_to_teacher: На ручну перевірку`. Also add the AI_REVIEW_FAILED badge branch to the status macro (red, `vocab.common.status_ai_review_failed: AI-рецензія не вдалася`).

- [ ] **Step 3: Verify + commit**

```bash
uv run --frozen --extra dev pytest tests/unit/test_state_machine.py tests/functional/test_teacher_portal.py tests/functional/test_teacher_portal_deep.py -q -o addopts="" && uv run --frozen ruff check src/ tests/ && uv run --frozen mypy src/
git add -A && git commit -m "Give teachers re-run, retry-AI and send-to-teacher controls

A submission that hit AI_REVIEW_FAILED five times, or a worker killed
mid-check, stayed stuck with no path out except a database edit.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: Bulk actions (C7)

**Files:**
- Modify: `src/submissions_checker/api/routes/teacher_portal.py` — extract `_apply_review_decision(db, submission, action, reason, current_user) -> str(event)` from `review_submission_post` (the approve/reject/has_quiz/finalize/outbox/audit block) and reuse it; add `POST /teacher/subjects/{subject_id}/assignments/{sa_id}/bulk` and `POST /teacher/students/resend-credentials`
- Modify: `templates/teacher_assignment.html` (checkbox column + bulk bar wrapping the table in one form), `templates/teacher_students.html` (checkbox column + "Надіслати доступи повторно" bar), `i18n/uk.yml`
- Test: `tests/functional/test_teacher_portal.py`, `tests/functional/test_teacher_portal_deep.py`

**Interfaces:**
- `POST …/bulk` form fields: `action` ∈ `approve|reject|rerun`, `submission_ids` (repeated), `reason` (reject). Applies to each id that belongs to this assignment and is in an eligible status; skips the rest; redirects to the board with `?bulk=<applied>,<skipped>`. Audit `bulk_review` with counts.
- `POST /teacher/students/resend-credentials` form field `student_ids` (repeated); for each student the caller may see (same scoping rule as `GET /teacher/students`): generate a new password, update `User.password_hash`, enqueue `SEND_CREDENTIALS` with the same payload shape as import; redirect to `/teacher/students?resent=<n>`. Audit `resend_credentials`.

- [ ] **Step 1: Failing tests**

```python
async def test_bulk_approve_completes_only_reviewable_rows(client, db, teacher, make_student) -> None:
    ... two students: one AWAITING_TEACHER_REVIEW, one COMPLETED; both enrolled
    r = await client.post(f"/teacher/subjects/{subject.id}/assignments/{sa.id}/bulk", data={"action": "approve", "submission_ids": [str(sub1.id), str(sub2.id)]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?bulk=1,1")
    → sub1 COMPLETED with grade finalized; sub2 unchanged


async def test_bulk_reject_requires_reason_and_records_it(...) -> None: ... FAILED + test_results check_reason


async def test_bulk_rerun_requeues(...) -> None: ... TEST_FAILED → PENDING + RUN_CHECKS outbox


async def test_bulk_ignores_submissions_of_other_assignments(...) -> None: ... skipped count


async def test_resend_credentials_rotates_password_and_enqueues_email(client, db, teacher, make_student, make_user) -> None:
    ... student enrolled in teacher's subject with a User; old hash captured
    r = await client.post("/teacher/students/resend-credentials", data={"student_ids": [str(student.id)]}, follow_redirects=False)
    assert r.status_code == 303
    await db.refresh(user); assert user.password_hash != old_hash
    outbox row SEND_CREDENTIALS with payload["student_email"] == student.email and payload["username"] == user.username


async def test_resend_credentials_skips_students_outside_scope(...) -> None: ... teacher who owns no subject with that student → n == 0
```

- [ ] **Step 2: Implement**

Refactor the single-review POST body into
```python
async def _apply_review_decision(db: AsyncSession, submission: Submission, action: str, reason: str | None, current_user: CurrentUserData) -> str:
    """Approve/reject one AWAITING_TEACHER_REVIEW submission; returns the event applied."""
    ...existing logic (has_quiz → teacher_send_quiz / teacher_approve; reject → teacher_reject; finalize_grade on teacher_approve; SUBMISSION_REVIEWED outbox; audit)...
```
and call it from `review_submission_post`. Bulk route:
```python
@router.post("/subjects/{subject_id}/assignments/{sa_id}/bulk")
async def bulk_board_action(subject_id: int, sa_id: int, db: DBSession, current_user: TeacherUser, action: str = Form(...), submission_ids: list[int] = Form(default=[]), reason: str = Form("")) -> RedirectResponse:
    await require_subject_access(db, subject_id, current_user)
    if action not in ("approve", "reject", "rerun"): raise HTTPException(400)
    if action == "reject" and not reason.strip(): raise HTTPException(422, "A reason is required")
    subs = (await db.execute(select(Submission).where(Submission.id.in_(submission_ids)).options(...loads...))).scalars().all()
    applied = skipped = 0
    for sub in subs:
        if sub.students_assignment.subjects_assignment_id != sa_id: skipped += 1; continue
        if action == "rerun":
            try: _requeue_checks(db, sub)  # the helper Task 8 extracted from rerun-checks
            except InvalidTransitionError: skipped += 1; continue
        elif sub.status != SubmissionStatus.AWAITING_TEACHER_REVIEW: skipped += 1; continue
        else: await _apply_review_decision(db, sub, action, reason, current_user)
        applied += 1
    await audit(db, action="bulk_review", actor_id=..., actor_username=..., subject_id=subject_id, sa_id=sa_id, bulk_action=action, applied=applied, skipped=skipped)
    await db.commit()
    return RedirectResponse(f"/teacher/subjects/{subject_id}/assignments/{sa_id}?bulk={applied},{skipped}", status_code=303)
```
(`_requeue_checks(db, submission)` in Task 8 must be a plain function that does the transition + unpin + outbox add, used by both routes — write it that way in Task 8.)

Resend credentials: scope query = the `GET /teacher/students` scoping (students enrolled in a subject the teacher owns; ADMIN all), then per student: `user = (select User where student_id)`; if none → skip; `password = _generate_password()`; `user.password_hash = bcrypt…`; enqueue outbox `SEND_CREDENTIALS` payload `{"student_email", "full_name", "username", "password"}` (copy the exact keys from the import block); count; audit `resend_credentials` with `student_ids`; commit; redirect `/teacher/students?resent=<n>`. `teacher_students` route: accept `resent: int = 0` and show a flash.

Templates: board — wrap `<table>` in `<form method="POST" action="…/bulk" id="bulk-form">`, add a first `<th>` with a select-all checkbox and a `<td><input type="checkbox" name="submission_ids" value="{{ row.submission_id }}"></td>` per row (disabled when no submission); above the table a bar with three buttons (`name="action" value="approve|reject|rerun"`) and a reason input shown next to reject; flash for `?bulk=`. Roster — same pattern with `student_ids` and one button. Vocab (`teacher:`): `bulk_select_all: Усі`, `bulk_approve_selected: Схвалити вибрані`, `bulk_reject_selected: Відхилити вибрані`, `bulk_rerun_selected: Перезапустити перевірку для вибраних`, `bulk_reason_placeholder: Причина відхилення`, `bulk_result: Застосовано {applied}, пропущено {skipped}` (render with `.replace` in the template or pass parts), `resend_credentials_selected: Надіслати доступи повторно`, `resent_result: Надіслано нові доступи`.

- [ ] **Step 3: Verify + commit**

```bash
uv run --frozen --extra dev pytest tests/functional/test_teacher_portal.py tests/functional/test_teacher_portal_deep.py tests/functional/test_portal_detail_pages.py -q -o addopts="" && uv run --frozen ruff check src/ tests/ && uv run --frozen mypy src/
git add -A && git commit -m "Add bulk approve/reject/re-run on the board and bulk credential resend

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 10: Cross-student similarity report (C8)

**Files:**
- Modify: `src/submissions_checker/services/similarity.py` (`token_set_for_zip(path) -> frozenset[str]`, `pairwise_similarity(items: dict[int, frozenset[str]]) -> list[tuple[int, int, float]]` sorted desc)
- Modify: `src/submissions_checker/api/routes/teacher_portal.py` (`GET /teacher/subjects/{subject_id}/assignments/{sa_id}/similarity?min=0.5`)
- Create: `templates/teacher_similarity.html`
- Modify: `templates/teacher_assignment.html` (link in the header), `i18n/uk.yml`
- Test: `tests/unit/test_similarity.py` (exists — extend), `tests/functional/test_teacher_portal_deep.py`

- [ ] **Step 1: Failing tests**

Unit:
```python
def test_pairwise_returns_sorted_pairs_above_zero() -> None:
    items = {1: frozenset({"a", "b", "c"}), 2: frozenset({"a", "b", "d"}), 3: frozenset({"x"})}
    pairs = pairwise_similarity(items)
    assert pairs[0][:2] == (1, 2) and abs(pairs[0][2] - 0.5) < 1e-9
    assert all(s >= 0 for _, _, s in pairs) and len(pairs) == 3
```
Functional: three students with latest ZIPs in `UPLOADS_DIR` (write small zips with near-identical `main.py` for two of them; the test file already has an upload helper for the plagiarism test — `test_submit_runs_plagiarism_comparison_against_prior_zip` in `test_portal_detail_pages.py` shows how `saved_as` is set); GET the report with `min=0.3` → the near-identical pair is listed with both names; the third is not.

- [ ] **Step 2: Implement**

`similarity.py` additions:
```python
def token_set_for_zip(zip_path: Path) -> frozenset[str]:
    return frozenset(_extract_tokens(zip_path))


def pairwise_similarity(items: dict[int, frozenset[str]]) -> list[tuple[int, int, float]]:
    """All unordered pairs with their Jaccard score, highest first."""
    keys = sorted(items)
    out = []
    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            sa, sb = items[a], items[b]
            union = len(sa | sb)
            score = len(sa & sb) / union if union else 0.0
            out.append((a, b, score))
    out.sort(key=lambda t: t[2], reverse=True)
    return out
```
Route: load the assignment (same access check as the board), latest submission per real enrolled student with `source_metadata.saved_as`, build `{student_assignment_id: token_set}` in a thread (`asyncio.to_thread`), compute pairs, keep `score >= min`, render rows `(student_a, student_b, pct)`; cap at 300 students (beyond that render a notice instead of computing). Template: table + `min` slider/input form (GET). Link on the board header: `vocab.teacher.similarity_report` → `…/similarity`. Vocab: `similarity_report: Звіт схожості`, `similarity_threshold: Поріг`, `similarity_no_pairs: Пар вище порогу немає`, `similarity_too_many: Забагато робіт для попарного порівняння`.

- [ ] **Step 3: Verify + commit**

```bash
uv run --frozen --extra dev pytest tests/unit/test_similarity.py tests/functional/test_teacher_portal_deep.py -q -o addopts="" && uv run --frozen ruff check src/ tests/ && uv run --frozen mypy src/
git add -A && git commit -m "Add a cross-student similarity report per assignment

The board showed one similarity number per row with no way to see who
matched whom.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 11: Docs, C4 clarification, audit bookkeeping, final gates

**Files:**
- Modify: `docs/PLUGIN_AUTHORING.md` (Review Modes: one sentence — a `quiz:` block under `tests_then_teacher` / `tests_then_ai_then_teacher` is sent automatically when the teacher approves; there is no separate `tests_then_teacher_then_quiz` mode)
- Modify: `docs/feature_catalog.md` (§1: remove the leftover `Internal user API` row; add change-password row; §3: resend credentials; §4: bulk, unstick, similarity rows; §7: deadline reminder row; §8: semesters), `docs/student_journey_guide.md` (drop "## 2. Choosing your language", add password change under "Logging in", renumber), `docs/teacher_journey_guide.md` (§6 unstick + bulk, §7 export button, new "Similarity report" subsection), `docs/admin_journey_guide.md` (semesters, done in Task 6), `docs/deployment.md` (note: login throttle is per replica; CSRF origin check relies on Caddy passing `Host`/`Origin` untouched)
- Modify: `docs/feature_audit.md` (✅ on C1, C11, B3, B5, B7, B8, B10, C7, C8, C4), `.claude/CLAUDE.md` (new modules: `core/rate_limit.py`, `core/csrf_middleware.py`, `workers/scheduled/deadline_reminders.py`; 5 scheduled jobs; new routes)
- Modify: `docs/known_bugs.md` (#1, #16 already in Task 7)

- [ ] **Step 1: Edit the docs as listed**
- [ ] **Step 2: Gates**

```bash
uv run --frozen ruff check src/ tests/ && uv run --frozen ruff format --check src/ tests/ && uv run --frozen mypy src/
uv run --frozen --extra dev pytest -q
```
- [ ] **Step 3: Commit**

```bash
git add -A docs/ && git commit -m "Document the new teacher/student features and close the audit items

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```
