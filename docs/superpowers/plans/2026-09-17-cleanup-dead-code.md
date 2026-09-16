# Cleanup: Dead Code, Retired Features, Stale Docs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove every dead/retired code path, orphan template, unused dependency, and stale document listed in `docs/feature_audit.md` §A, plus the three "finalize by deleting" items (B4 drop language selector, B6 reject `SHORT_ANSWER`, B11 seed hygiene) and the README rewrite (B12).

**Architecture:** Pure subtraction plus one Alembic migration (`0027`) that maps historical enum values to their current equivalents and drops the retired values. Nothing new is built; every task ends with the full suite green. Tasks are ordered so each is independently committable and later tasks never reintroduce something an earlier task deleted.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, pytest (unit / integration / functional via testcontainers), ruff, mypy, uv.

**Spec:** `docs/feature_audit.md` sections A1–A9, B4, B6, B11, B12, D. User decisions (2026-09-17): drop the language selector rather than add `en.yml`; reject `SHORT_ANSWER` rather than build manual grading; B9 (air-raid reaper) is explicitly OUT of scope.

## Global Constraints

- Run everything with `uv run --frozen …`. Never run `uv run` without `--frozen` except in the one task that edits `pyproject.toml` (Task 1), where `uv lock` is intentional.
- Repo is clean at HEAD: `uv run --frozen ruff check src/ tests/`, `uv run --frozen ruff format --check src/ tests/`, `uv run --frozen mypy src/`, and `uv run --frozen --extra dev pytest -q` (805 tests, ~6 min, needs Docker) all pass before Task 1. Every task must leave them passing.
- `SubmissionStatus`, `SubmissionSourceType`, `OutboxEventType` are native PostgreSQL enums named `submission_status`, `submission_source_type`, `outbox_event_type` (created in `alembic/versions/0001_initial_schema.py`). PostgreSQL cannot `DROP VALUE` from an enum; removing values means rename-type / create-new / alter-column / drop-old.
- `OutboxEventType.NEW_SUBMISSION` and its no-op handler `execute_new_submission_task` are **kept**: eleven tests across `test_workers.py`, `test_database.py`, `test_worker_tasks.py`, `test_metrics.py` use it as the canonical harmless event. Deleting it buys nothing.
- UI strings live in `i18n/uk.yml` and are referenced as `vocab.<section>.<key>`. Deleting a template means deleting the keys only it used.
- Commit messages: imperative subject ≤ 72 chars, body explains *why*, trailer `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Dev DB (docker compose `postgres`) holds only seed data; production DB state is unknown — the migration in Task 7 must be safe on rows it has not seen (it maps, never asserts-empty).

---

### Task 1: Remove GitHub dependency, dead email templates, dead `services/github/` dir

**Files:**
- Modify: `pyproject.toml` (dependencies list, line with `"pygithub>=2.5.0",`)
- Modify: `uv.lock` (regenerated)
- Modify: `src/submissions_checker/main.py:103`
- Modify: `src/submissions_checker/services/notifications/templates.py:134-165` (delete `passed_template`, `failed_template`), `:74-91` (delete `new_submission_template`)
- Modify: `tests/unit/test_notifications.py:98-103` (delete `test_new_submission_template_addresses_teacher`), `:111-118` (delete `test_quiz_pass_fail_github_templates`)
- Delete: `src/submissions_checker/services/github/` (only a `CLAUDE.md` stub + `__pycache__`)

**Interfaces:**
- Consumes: nothing.
- Produces: `notifications/templates.py` exports exactly `submission_reviewed_template, quiz_result_template, deadline_reminder_template, teacher_digest_template, password_reset_template, feedback_request_template, credentials_template, quiz_dispute_resolved_template`.

- [ ] **Step 1: Prove the three templates have no callers**

Run:
```bash
grep -rn 'new_submission_template\|passed_template\|failed_template' src/
```
Expected: only the three `def` lines inside `services/notifications/templates.py`.

- [ ] **Step 2: Delete the three template functions**

In `src/submissions_checker/services/notifications/templates.py` delete the whole bodies of `new_submission_template` (lines 74–91), `passed_template` (134–148) and `failed_template` (149–165). Leave every other function untouched.

- [ ] **Step 3: Delete their unit tests**

In `tests/unit/test_notifications.py` delete `test_new_submission_template_addresses_teacher` (lines 98–103) and the parametrized `test_quiz_pass_fail_github_templates` (111–118, including the `@pytest.mark.parametrize` decorator).

- [ ] **Step 4: Run the unit file**

Run: `uv run --frozen --extra dev pytest tests/unit/test_notifications.py -q`
Expected: PASS, 2 fewer tests than before.

- [ ] **Step 5: Remove `pygithub` and relock**

In `pyproject.toml` delete the line `    "pygithub>=2.5.0",`. Then:
```bash
uv lock
git diff --stat uv.lock
```
Expected: `uv.lock` loses the `pygithub` package (and its transitive-only deps such as `pynacl`, `deprecated`, `wrapt` if nothing else needs them). Confirm no `github` import exists:
```bash
grep -rn '^from github\|^import github' src/ tests/
```
Expected: no output.

- [ ] **Step 6: Fix the app description and delete the empty dir**

In `src/submissions_checker/main.py` change
```python
        description="Automated student code submission checker with GitHub integration",
```
to
```python
        description="Automated student code submission checker: ZIP upload, sandboxed checks, AI/teacher/quiz review",
```
Then:
```bash
rm -rf src/submissions_checker/services/github
```

- [ ] **Step 7: Lint, type-check, run unit tests**

Run:
```bash
uv run --frozen ruff check src/ tests/ && uv run --frozen ruff format --check src/ tests/ && uv run --frozen mypy src/ && uv run --frozen --extra dev pytest tests/unit -q
```
Expected: all clean, unit tests PASS.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock src/submissions_checker/main.py src/submissions_checker/services/notifications/templates.py tests/unit/test_notifications.py
git commit -m "Remove pygithub and the last GitHub-era email templates

The PR-ingest pipeline was retired in June; pygithub had no importer left
and the three 'Hi @github_username' templates had no caller. Dropping them
stops the dependency tree and the templates module advertising a feature
that no longer exists.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Remove `github_username` / `github_repo` from the application surface

**Files:**
- Modify: `src/submissions_checker/api/routes/teacher_portal.py:430` (drop column from select), `:1365` (drop Form param), `:1397` (drop kwarg)
- Modify: `src/submissions_checker/api/schemas/student_portal.py:13-19` (delete `StudentListItem`)
- Modify: `src/submissions_checker/services/config_apply.py:265`, `:447`, `:564` (drop `github_repo` mapping)
- Modify: `src/submissions_checker/db/models/student.py:28` (delete column), `src/submissions_checker/db/models/subject.py:47` (delete column)
- Modify: `templates/teacher_add_student.html:50-55` (delete the field block), `templates/teacher_assignment.html:92` and `:104-106` (delete GitHub column header + cell), `templates/teacher_submission_review.html:39` (delete the `@handle` line)
- Modify: `i18n/uk.yml:305` (`col_github`), `:404` (`github_username_label`) — delete both keys
- Modify: `tests/functional/conftest.py:183,192` (drop `github_username` param), `tests/functional/test_teacher_portal_deep.py:1062,1073`, `tests/functional/test_apply_config.py:55`, `tests/integration/test_config_apply.py:88,129`, `tests/unit/test_config_apply_helpers.py:163`
- Note: the DB columns are dropped by Task 7's migration `0027`. This task removes them from the ORM only; SQLAlchemy ignores columns the model does not declare, so the app runs fine in between.

**Interfaces:**
- Consumes: nothing.
- Produces: `Student` has no `github_username`; `Subject` has no `github_repo`; `POST /teacher/students/add` accepts `first_name, last_name, email, group_name` only; `config.yml` key `githubRepo` is silently ignored.

- [ ] **Step 1: Write the failing functional test for the add-student form**

Append to `tests/functional/test_teacher_portal_deep.py`:
```python
async def test_add_student_form_has_no_github_field(teacher_client: AsyncClient) -> None:
    """The GitHub handle field was retired with the PR ingest; the form must not render it."""
    resp = await teacher_client.get("/teacher/students/add")
    assert resp.status_code == 200
    assert 'name="github_username"' not in resp.text
```
Run: `uv run --frozen --extra dev pytest tests/functional/test_teacher_portal_deep.py::test_add_student_form_has_no_github_field -q`
Expected: FAIL (field is rendered).

- [ ] **Step 2: Edit templates**

`templates/teacher_add_student.html` — delete lines 50–55:
```html
    <div>
      <label class="block text-sm font-medium text-slate-700 mb-1" for="github_username">{{ vocab.teacher.github_username_label }}</label>
      <input id="github_username" name="github_username" type="text"
             class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"/>
    </div>
```
`templates/teacher_assignment.html` — delete line 92 (`<th …>{{ vocab.teacher.col_github }}</th>`) and lines 104–106 (the `<td>` containing `@{{ row.github_username }}`).
`templates/teacher_submission_review.html` — delete line 39 (`<p class="text-xs text-slate-400">@{{ student.github_username }}</p>`).

- [ ] **Step 3: Edit the route**

`src/submissions_checker/api/routes/teacher_portal.py`:
- line 430: delete `            Student.github_username,`
- line 1365: delete `    github_username: str = Form(""),`
- line 1397: delete `        github_username=github_username.strip() or None,`

- [ ] **Step 4: Edit models, schema, config_apply**

`src/submissions_checker/db/models/student.py` — delete line 28 (`github_username: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)`). If `String` is now unused in the import, remove it from the import.
`src/submissions_checker/db/models/subject.py` — delete line 47 (`github_repo: …`).
`src/submissions_checker/api/schemas/student_portal.py` — delete the whole `StudentListItem` class (lines 13–19). Verify: `grep -rn StudentListItem src/ tests/` → no output.
`src/submissions_checker/services/config_apply.py`:
- line 265: delete `            "github_repo": ("github_repo", new_cfg.get("githubRepo") or None),`
- line 447: delete `                github_repo=new_cfg.get("githubRepo") or None,`
- line 564: delete `            "github_repo": new_cfg.get("githubRepo") or None,`

- [ ] **Step 5: Edit i18n and tests**

`i18n/uk.yml` — delete the `col_github: GitHub` line (305) and the `github_username_label: …` line (404).
`tests/functional/conftest.py` — remove the `github_username: str | None = None` parameter of the student factory (line 183) and the `github_username=github_username,` kwarg (192).
`tests/functional/test_teacher_portal_deep.py` — at line 1062 delete `"github_username": "aturing",` from the form payload and at 1073 delete `assert student.github_username == "aturing"`.
`tests/functional/test_apply_config.py:55` and `tests/integration/test_config_apply.py:88` — delete the `"githubRepo": "org/demo",` entries. `tests/integration/test_config_apply.py:129` — delete `assert subject.github_repo == "org/demo"`.
`tests/unit/test_config_apply_helpers.py:163` — delete `self.github_repo = kw.get("github_repo")`.

- [ ] **Step 6: Run the touched suites**

Run:
```bash
uv run --frozen --extra dev pytest tests/functional/test_teacher_portal_deep.py tests/functional/test_teacher_portal.py tests/functional/test_apply_config.py tests/integration/test_config_apply.py tests/unit/test_config_apply_helpers.py tests/functional/test_portal_detail_pages.py -q
```
Expected: PASS including the new test. Then `uv run --frozen ruff check src/ tests/ && uv run --frozen mypy src/`.

- [ ] **Step 7: Commit**

```bash
git add -A src/ templates/ i18n/ tests/
git commit -m "Drop github_username and github_repo from the app surface

Nothing displayed or set them after the PR ingest was retired: CSV import
never wrote github_username and no page ever showed github_repo. The ORM
columns go now; the DB columns are dropped in the 0027 migration together
with the retired enum values.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Delete never-wired service stubs and stale scripts

**Files:**
- Delete: `src/submissions_checker/services/user_service.py`, `src/submissions_checker/services/testing/` (whole package: `__init__.py`, `runner.py`, `result_parser.py`, `CLAUDE.md`), `src/submissions_checker/services/submission_checker.py`
- Delete: `tests/unit/test_user_service.py`, `tests/unit/test_testing_runner.py`, `tests/unit/test_result_parser.py`
- Delete: `scripts/init_db.py` (uses `Base.metadata.create_all`, bypasses Alembic), `scripts/quiz_form.gs` (Google-Forms quiz era)
- Modify: `tests/README.md` — remove "parsers, service modules (external SDKs mocked)" wording if it names these files

**Interfaces:**
- Consumes: nothing.
- Produces: `services/` contains no `NotImplementedError` stubs.

- [ ] **Step 1: Prove zero callers**

```bash
grep -rn 'user_service\|services\.testing\|submission_checker import\|from submissions_checker.services.submission_checker' src/
```
Expected: no output (only the files themselves, which are being deleted).

- [ ] **Step 2: Delete**

```bash
git rm -r src/submissions_checker/services/user_service.py src/submissions_checker/services/testing src/submissions_checker/services/submission_checker.py tests/unit/test_user_service.py tests/unit/test_testing_runner.py tests/unit/test_result_parser.py scripts/init_db.py scripts/quiz_form.gs
rm -rf src/submissions_checker/services/testing   # leftover __pycache__ / CLAUDE.md
```

- [ ] **Step 3: Grep tests/README for the deleted names**

```bash
grep -n 'parser\|user_service\|runner' tests/README.md
```
If line ~9 says "…similarity, parsers, notification templates, service modules (external SDKs mocked)…", change to "…similarity, notification templates, the AI provider client (SDK mocked)…".

- [ ] **Step 4: Verify**

Run: `uv run --frozen ruff check src/ tests/ && uv run --frozen mypy src/ && uv run --frozen --extra dev pytest tests/unit -q`
Expected: clean, PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "Delete unimplemented service stubs and pre-Alembic scripts

user_service, testing/runner, testing/result_parser and submission_checker
were TODO skeletons with zero callers; the live pipeline is check_core +
docker_sandbox. init_db.py used create_all around Alembic and quiz_form.gs
belonged to the Google-Forms quiz flow that no longer exists.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Delete the fake `/api/v1/users` router and unused settings

**Files:**
- Delete: `src/submissions_checker/api/routes/users.py`
- Modify: `src/submissions_checker/main.py` (import list line ~22 `users,` and `app.include_router(users.router)`)
- Modify: `src/submissions_checker/core/config.py` — delete `api_v1_prefix` (line 27), `base_url` (line 39 + its comment), `ai_temperature` (line 49)
- Modify: `.env.example` — delete `AI_TEMPERATURE=0.7` and `API_V1_PREFIX=/api/v1`
- Modify: `tests/functional/test_coverage_gaps.py:334-360` (delete the users.py section: `test_get_user_skeleton_returns_not_implemented`, the not-a-number test, `test_create_user_skeleton_returns_not_implemented`; also the docstring bullet at line 13)
- Modify: `tests/integration/test_api.py:75` (delete the `/api/v1/users` POST test)

**Interfaces:**
- Produces: `GET/POST /api/v1/users*` → 404.

- [ ] **Step 1: Write the failing test**

Append to `tests/functional/test_coverage_gaps.py`:
```python
async def test_api_v1_users_is_gone(client: AsyncClient) -> None:
    """The skeleton users API returned 200 + not_implemented; it must now 404."""
    assert (await client.get("/api/v1/users/1")).status_code == 404
    assert (await client.post("/api/v1/users")).status_code == 404
```
Run: `uv run --frozen --extra dev pytest tests/functional/test_coverage_gaps.py::test_api_v1_users_is_gone -q`
Expected: FAIL (200).

- [ ] **Step 2: Delete router + mount**

```bash
git rm src/submissions_checker/api/routes/users.py
```
In `main.py` remove `    users,` from the `from submissions_checker.api.routes import (…)` block and delete `    app.include_router(users.router)`.

- [ ] **Step 3: Delete settings**

In `core/config.py` delete:
```python
    # API
    api_v1_prefix: str = "/api/v1"
```
(keep `cors_origins` under a `# API` comment), and
```python
    # Backend base URL (used to build callback URLs for external services)
    base_url: str = "http://localhost:8000"
```
and `    ai_temperature: float = 0.7`. Confirm: `grep -rn 'api_v1_prefix\|ai_temperature\|settings\.base_url' src/ tests/` → nothing.
In `.env.example` delete the `AI_TEMPERATURE=0.7` and `API_V1_PREFIX=/api/v1` lines.

- [ ] **Step 4: Delete old tests**

`tests/functional/test_coverage_gaps.py`: delete lines 334–360 (section header comment through the end of `test_create_user_skeleton_returns_not_implemented`) and the docstring bullet on line 13. `tests/integration/test_api.py`: delete the test around line 75 that posts to `/api/v1/users` (whole function).

- [ ] **Step 5: Verify**

Run: `uv run --frozen --extra dev pytest tests/functional/test_coverage_gaps.py tests/integration/test_api.py tests/unit/test_config.py -q && uv run --frozen ruff check src/ tests/ && uv run --frozen mypy src/`
Expected: PASS incl. new test.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "Remove the fake /api/v1/users endpoints and dead settings

Both handlers answered 200 with {\"status\": \"not_implemented\"} in
production (known bug #5). api_v1_prefix, base_url and ai_temperature had
no reader.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Delete orphan templates and their vocabulary keys

**Files:**
- Delete: `templates/teacher_assignment_form.html`, `templates/teacher_quiz_editor.html`, `templates/student_select.html`
- Modify: `i18n/uk.yml` — delete every key used only by those templates (list below)
- Modify: `tests/unit/test_i18n.py` — only if a test enumerates the keys (it does not; `test_shipped_vocabularies_have_only_string_keys` is structural)

- [ ] **Step 1: Prove they are never rendered**

```bash
grep -rn 'teacher_assignment_form\|teacher_quiz_editor\|student_select' src/ templates/ tests/
```
Expected: no output.

- [ ] **Step 2: Delete the templates**

```bash
git rm templates/teacher_assignment_form.html templates/teacher_quiz_editor.html templates/student_select.html
```

- [ ] **Step 3: Delete now-unused vocab keys**

Run this to list keys with zero remaining references, then delete each from `i18n/uk.yml`:
```bash
for k in $(grep -ohP '^  [a-z_0-9]+(?=:)' i18n/uk.yml | tr -d ' ' | sort -u); do
  n=$(grep -rn "\.$k\b" templates/ src/ | wc -l); [ "$n" -eq 0 ] && echo "$k"; done
```
Expected candidates (verify the script agrees before deleting): under `common:` `cannot_be_undone`, `danger_zone`; under `student:` `no_students`, `no_students_hint`, `page_select_title`, `pick_name`, `students_label`, `who_are_you`; under `teacher:` `add_item`, `add_option`, `add_question_button`, `add_question_title`, all `anticheat_*` editor keys (`anticheat_add_rule … anticheat_title`, 20 keys), `assignment_create`, `assignment_deadline_label`, `assignment_delete`, `assignment_delete_warning`, `assignment_description_label`, `assignment_form_heading_create`, `assignment_form_heading_edit`, `assignment_form_title_create`, `assignment_form_title_edit`, `assignment_late_allow`, `assignment_late_block`, `assignment_late_policy_label`, `assignment_max_grade_label`, `assignment_max_submissions_label`, `assignment_max_submissions_placeholder`, `assignment_min_grade_label`, `assignment_review_mode_label`, `assignment_review_none`, `assignment_review_quiz`, `assignment_review_teacher`, `assignment_save`, `assignment_title_label`, `create_quiz`, `delete_question`, `edit_question`, `max_attempts_hint`, `max_attempts_label`, `mc_options_label`, `no_questions`, `no_quiz_settings`, `ordering_items_hint`, `ordering_items_label`, `pass_threshold_hint`, `pass_threshold_label`, `question_cancel`, `question_config_label`, `question_points_label`, `question_required_label`, `question_save`, `question_sort_order_label`, `questions_per_quiz_hint`, `questions_per_quiz_label`, `questions_section_title`, `question_text_label`, `question_type_label`, `quiz_editor_title`, `quiz_export_json`, `quiz_import_json`, `quiz_settings`, `required_badge`, `sc_options_label`, `show_correct_after`, `shuffle_options`, `shuffle_questions`, `tf_correct_label`, `time_limit_hint`, `time_limit_label`, `type_multiple_choice`, `type_ordering`, `type_short_answer`, `type_single_choice`, `type_true_false`, `update_settings`.
**Do not delete** a key the script still finds referenced (e.g. `true_false` labels used by `student_quiz.html`; `test_shipped_vocabularies_define_the_true_false_labels` guards those).

- [ ] **Step 4: Verify the vocabulary still loads and every page renders**

Run: `uv run --frozen --extra dev pytest tests/unit/test_i18n.py tests/functional -q`
Expected: PASS (functional tests render every live template; a deleted-but-used key would raise `UndefinedError` in dev or render empty — `DebugUndefined` is on in development, tests run with `ENVIRONMENT=test`, so grep is the guard here, not the tests).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "Delete three templates no route ever rendered

teacher_assignment_form and teacher_quiz_editor posted to routes that never
existed (known bug #10); student_select was the pre-login student picker.
Their vocabulary keys go with them.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Drop the unreachable language selector (audit B4)

**Files:**
- Delete: `src/submissions_checker/api/routes/i18n.py`
- Modify: `src/submissions_checker/main.py` (remove `i18n,` import and `app.include_router(i18n.router)`)
- Modify: `src/submissions_checker/core/i18n.py` (delete `AVAILABLE_LANGUAGES` and the code that fills it; keep `DEFAULT_LANG`, `load_vocabularies`, `get_vocab`)
- Modify: `src/submissions_checker/core/templates.py` (drop `AVAILABLE_LANGUAGES` import and the `"available_languages"` context key)
- Modify: `templates/base.html:62-74` (delete the `{% if available_languages | length > 1 %} … {% endif %}` block)
- Modify: `tests/functional/test_i18n.py` (delete the four `test_set_language_*` tests; keep any vocab-rendering tests), `tests/unit/test_i18n.py:43-63` (`test_load_vocabularies_registers_languages_and_labels` and `…_label_falls_back_to_code` assert on `AVAILABLE_LANGUAGES` — rewrite to assert on `get_vocab` instead)
- Modify: `docs/feature_catalog.md` §1 — delete the "Choose interface language" row

**Interfaces:**
- Produces: `core.i18n` exports `DEFAULT_LANG: str`, `load_vocabularies(vocab_dir: Path) -> None`, `get_vocab(lang_cookie: str | None) -> dict[str, Any]`. `POST /set-language` → 404.

- [ ] **Step 1: Failing test**

Append to `tests/functional/test_i18n.py`:
```python
async def test_set_language_route_is_gone(client: AsyncClient) -> None:
    resp = await client.post("/set-language", data={"lang": "uk"})
    assert resp.status_code == 404
```
Run it; expected FAIL (302/400).

- [ ] **Step 2: Remove route, mount, selector**

```bash
git rm src/submissions_checker/api/routes/i18n.py
```
`main.py`: drop `    i18n,` from the routes import and the `app.include_router(i18n.router)` line.
`templates/base.html`: delete lines 62–74 (from `{% if available_languages | length > 1 %}` through its `{% endif %}`).

- [ ] **Step 3: Simplify `core/i18n.py`**

Replace the file body with:
```python
"""Vocabulary loader and per-request language resolver."""

from pathlib import Path
from typing import Any

import yaml

_VOCABULARIES: dict[str, dict[str, Any]] = {}

DEFAULT_LANG = "uk"


def load_vocabularies(vocab_dir: Path) -> None:
    """Scan vocab_dir for *.yml files and register each by file stem.

    The first file in sorted order becomes the default language.
    """
    global DEFAULT_LANG
    _VOCABULARIES.clear()

    if not vocab_dir.exists():
        return

    for path in sorted(vocab_dir.glob("*.yml")):
        with open(path, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        _VOCABULARIES[path.stem] = data

    if _VOCABULARIES:
        DEFAULT_LANG = next(iter(_VOCABULARIES))


def get_vocab(lang_cookie: str | None) -> dict[str, Any]:
    """Return the vocabulary for the requested language, falling back to the default."""
    if not _VOCABULARIES:
        return {}
    if lang_cookie and lang_cookie in _VOCABULARIES:
        return _VOCABULARIES[lang_cookie]
    return _VOCABULARIES.get(DEFAULT_LANG, next(iter(_VOCABULARIES.values())))
```
`core/templates.py`: change the import to `from submissions_checker.core.i18n import get_vocab` and delete the `"available_languages": AVAILABLE_LANGUAGES,` line.

- [ ] **Step 4: Fix unit tests**

In `tests/unit/test_i18n.py` rewrite the two tests that read `AVAILABLE_LANGUAGES`:
```python
def test_load_vocabularies_registers_languages(tmp_path: Path) -> None:
    (tmp_path / "uk.yml").write_text("_meta:\n  label: Українська\nnav:\n  x: y\n", encoding="utf-8")
    (tmp_path / "en.yml").write_text("nav:\n  x: z\n", encoding="utf-8")
    i18n.load_vocabularies(tmp_path)
    assert i18n.get_vocab("uk")["nav"]["x"] == "y"
    assert i18n.get_vocab("en")["nav"]["x"] == "z"
    assert i18n.DEFAULT_LANG == "en"  # sorted order: en < uk
```
Delete `test_load_vocabularies_label_falls_back_to_code` (labels no longer exist). Delete the four `test_set_language_*` functions from `tests/functional/test_i18n.py`.

- [ ] **Step 5: Verify**

Run: `uv run --frozen --extra dev pytest tests/unit/test_i18n.py tests/functional/test_i18n.py -q && uv run --frozen ruff check src/ tests/ && uv run --frozen mypy src/`
Expected: PASS.

- [ ] **Step 6: Update the catalogue and commit**

In `docs/feature_catalog.md` delete the row `| Choose interface language (English / Ukrainian; remembered ~1 year) | Anyone | \`POST /set-language\` |`.
```bash
git add -A
git commit -m "Remove the language selector; the UI ships Ukrainian only

Only uk.yml was ever committed, so base.html hid the selector and
/set-language was unreachable. The vocabulary loader stays (templates
still read vocab.*); the route, the language list and the cookie form go.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Migration 0027 — retire legacy enum values and GitHub columns

**Files:**
- Create: `alembic/versions/0027_retire_legacy_enums.py`
- Modify: `src/submissions_checker/db/models/enums.py` (delete `PROCESSING, REVIEWING, CHECKING, CHECK_FAILED, WAITING_FOR_TEACHER_REVIEW` from `SubmissionStatus`; delete `GITHUB_PR, GITLAB_MR` from `SubmissionSourceType`; delete `PULL, REVIEW, NOTIFY` from `OutboxEventType`; delete the DEPRECATED comments)
- Modify: `src/submissions_checker/core/state_machine.py` (delete `"start_check"` from PENDING, and the whole `# ── Legacy flow` section: `CHECKING` and `WAITING_FOR_TEACHER_REVIEW` entries)
- Modify: `src/submissions_checker/workers/scheduled/outbox_processor.py:30-33` (delete `_RETIRED_EVENT_TYPES`) and the branch that uses it (~lines 162–170 `if message.event_type in _RETIRED_EVENT_TYPES:`)
- Modify: `templates/teacher_assignment.html:23-43,109`, `templates/assignments.html:23-31` (delete legacy status branches)
- Modify: `tests/unit/test_state_machine.py` (remove legacy rows from `LEGAL` — lines 29, 51–57; line 88 `(S.CHECKING, "teacher_approve")`; lines 117–120 PROCESSING test), `tests/functional/test_portal_detail_pages.py:447-495,626-640`, `tests/functional/test_teacher_portal.py:404-415`, `tests/e2e/helpers.py:28`, `tests/integration/test_workers.py:128-170` (`test_outbox_processor_drops_retired_event_type_without_retry` uses `OutboxEventType.PULL` — delete the test)
- Modify: `docs/feature_catalog.md` (remove the "legacy flow" paragraph under the state machine and the "ZIP only … enum members remain" convention line), `docs/known_bugs.md` #8 (append "Retired values dropped in 0027")
- Modify: `alembic/versions/0002_dummy_data.py:56,66,276` — stop seeding `github_username`, `github_repo`, and `source_type='GITHUB_PR'` (on a fresh DB 0002 runs before 0027, so the columns exist, but seeding values that 0027 then rewrites is pointless)

**Interfaces:**
- Produces: `SubmissionStatus` = `PENDING VALIDATING VALIDATION_FAILED TESTING TEST_FAILED AWAITING_AI_REVIEW AI_REVIEWING AI_REVIEW_FAILED AWAITING_TEACHER_REVIEW QUIZ_SENT COMPLETED FAILED`; `SubmissionSourceType` = `ZIP_UPLOAD`; `OutboxEventType` = `SEND_CREDENTIALS SUBMISSION_REVIEWED QUIZ_RESULT DEADLINE_REMINDER NEW_SUBMISSION RUN_CHECKS RUN_AI_REVIEW FEEDBACK_REQUEST_SENT QUIZ_DISPUTE_RESOLVED`.

- [ ] **Step 1: Write the failing state-machine test**

In `tests/unit/test_state_machine.py` add:
```python
def test_legacy_statuses_are_gone() -> None:
    for name in ("PROCESSING", "REVIEWING", "CHECKING", "CHECK_FAILED", "WAITING_FOR_TEACHER_REVIEW"):
        assert not hasattr(S, name), name


def test_start_check_alias_is_gone() -> None:
    sub = _Sub(S.PENDING)
    with pytest.raises(InvalidTransitionError):
        transition(sub, "start_check")
```
Run: `uv run --frozen --extra dev pytest tests/unit/test_state_machine.py -q` → FAIL.

- [ ] **Step 2: Write the migration**

Create `alembic/versions/0027_retire_legacy_enums.py`:
```python
"""Retire legacy submission statuses, GitHub source types and outbox events.

Revision ID: 0027
Revises: 0026

The GitHub-PR ingest and the pre-2026-06 "CHECKING" flow are gone from the code.
Historical rows are mapped to their closest current value before the enum
values are removed, so this is safe on any database regardless of contents.
PostgreSQL cannot drop an enum value, hence the rename/recreate dance.
"""

from __future__ import annotations

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels = None
depends_on = None

_STATUS_MAP = {
    "PROCESSING": "VALIDATING",
    "CHECKING": "TESTING",
    "REVIEWING": "AWAITING_AI_REVIEW",
    "CHECK_FAILED": "TEST_FAILED",
    "WAITING_FOR_TEACHER_REVIEW": "AWAITING_TEACHER_REVIEW",
}
_STATUS_VALUES = (
    "PENDING", "VALIDATING", "VALIDATION_FAILED", "TESTING", "TEST_FAILED",
    "AWAITING_AI_REVIEW", "AI_REVIEWING", "AI_REVIEW_FAILED",
    "AWAITING_TEACHER_REVIEW", "QUIZ_SENT", "COMPLETED", "FAILED",
)
_EVENT_VALUES = (
    "SEND_CREDENTIALS", "SUBMISSION_REVIEWED", "QUIZ_RESULT", "DEADLINE_REMINDER",
    "NEW_SUBMISSION", "RUN_CHECKS", "RUN_AI_REVIEW", "FEEDBACK_REQUEST_SENT",
    "QUIZ_DISPUTE_RESOLVED",
)


def _recreate_enum(type_name: str, table: str, column: str, values: tuple[str, ...]) -> None:
    quoted = ", ".join(f"'{v}'" for v in values)
    op.execute(f"ALTER TYPE {type_name} RENAME TO {type_name}_old")
    op.execute(f"CREATE TYPE {type_name} AS ENUM ({quoted})")
    op.execute(
        f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {type_name} "
        f"USING {column}::text::{type_name}"
    )
    op.execute(f"DROP TYPE {type_name}_old")


def upgrade() -> None:
    # 1. submissions.status — map legacy values, then recreate the enum.
    for old, new in _STATUS_MAP.items():
        op.execute(f"UPDATE submissions SET status = '{new}' WHERE status = '{old}'")
    op.execute("ALTER TABLE submissions ALTER COLUMN status DROP DEFAULT")
    _recreate_enum("submission_status", "submissions", "status", _STATUS_VALUES)
    op.execute("ALTER TABLE submissions ALTER COLUMN status SET DEFAULT 'PENDING'")

    # 2. submissions.source_type — everything is a ZIP upload now.
    op.execute("UPDATE submissions SET source_type = 'ZIP_UPLOAD' WHERE source_type <> 'ZIP_UPLOAD'")
    _recreate_enum("submission_source_type", "submissions", "source_type", ("ZIP_UPLOAD",))

    # 3. outbox_messages.event_type — stray retired rows carry no work; delete them.
    op.execute("DELETE FROM outbox_messages WHERE event_type IN ('PULL', 'REVIEW', 'NOTIFY')")
    _recreate_enum("outbox_event_type", "outbox_messages", "event_type", _EVENT_VALUES)

    # 4. GitHub-era columns.
    op.execute("DROP INDEX IF EXISTS ix_students_github_username")
    op.execute("ALTER TABLE students DROP CONSTRAINT IF EXISTS students_github_username_key")
    op.drop_column("students", "github_username")
    op.drop_column("subjects", "github_repo")


def downgrade() -> None:
    raise RuntimeError("0027 is irreversible: legacy enum values were mapped away")
```
Before running, check the real default/index names on the dev DB and adjust the two `DROP … IF EXISTS` lines:
```bash
docker compose exec -T postgres psql -U postgres -d submissions_checker -c "\d students" -c "\d submissions" | grep -i 'github\|status.*default'
```

- [ ] **Step 3: Run the migration on the dev DB**

```bash
docker compose exec -T app alembic upgrade head
docker compose exec -T postgres psql -U postgres -d submissions_checker -c "select unnest(enum_range(null::submission_status))" -c "select unnest(enum_range(null::submission_source_type))" -c "select unnest(enum_range(null::outbox_event_type))" -c "\d students" | grep -c github
```
Expected: the three enum lists match the `Produces` block; `github` count = 0.

- [ ] **Step 4: Remove the values from code**

`enums.py` — delete the five legacy `SubmissionStatus` members and the "Legacy values" comment; delete `GITHUB_PR`, `GITLAB_MR` and the DEPRECATED comment; delete `PULL`, `REVIEW`, `NOTIFY` and their comment.
`state_machine.py` — delete `"start_check": SubmissionStatus.CHECKING,` and its comment from the PENDING entry; delete everything from `# ── Legacy flow` to the end of the `WAITING_FOR_TEACHER_REVIEW` entry.
`outbox_processor.py` — delete `_RETIRED_EVENT_TYPES` (with its comment) and the `if message.event_type in _RETIRED_EVENT_TYPES:` branch (the whole block that logs `outbox_retired_event_type_dropped` and pre-exhausts retries).
`templates/teacher_assignment.html` — in the status-badge macro delete the `{% elif status == "CHECKING" %}`, `{% elif status == "CHECK_FAILED" %}`, `{% elif status == "WAITING_FOR_TEACHER_REVIEW" %}` branches; change `{% elif status in ["REVIEWING", "QUIZ_SENT"] %}` to `{% elif status == "QUIZ_SENT" %}`; change `{% elif status in ["PENDING", "PROCESSING"] %}` to `{% elif status == "PENDING" %}` and drop the inner `{% if status == "PROCESSING" %}…{% else %}` so only `vocab.common.status_submitted` remains; at line ~109 change `row.submission_status == "WAITING_FOR_TEACHER_REVIEW"` to `row.submission_status == "AWAITING_TEACHER_REVIEW"` only if the same condition does not already exist for `AWAITING_TEACHER_REVIEW` (if it does, delete the legacy branch).
`templates/assignments.html` — same three edits (lines 23–31).
`alembic/versions/0002_dummy_data.py` — line 56: drop `github_username` from the INSERT column list and its value; line 66: drop `github_repo`; line 276: use `'ZIP_UPLOAD'` for every seeded submission.

- [ ] **Step 5: Fix tests**

`tests/unit/test_state_machine.py`: delete `LEGAL` rows at lines 29 and 51–57; delete `(S.CHECKING, "teacher_approve")` at 88; delete the PROCESSING test at 117–120.
`tests/functional/test_portal_detail_pages.py`: the two tests at 447–495 exercise the legacy `WAITING_FOR_TEACHER_REVIEW` approve/reject path — delete them (the `AWAITING_TEACHER_REVIEW` equivalents exist in `test_teacher_portal.py`); at 626–640 change `SubmissionStatus.CHECK_FAILED` to `SubmissionStatus.TEST_FAILED`.
`tests/functional/test_teacher_portal.py:404-415`: delete the "Legacy review status path" test.
`tests/e2e/helpers.py:28`: remove `"CHECK_FAILED"` from the set.
`tests/integration/test_workers.py`: delete `test_outbox_processor_drops_retired_event_type_without_retry`.

- [ ] **Step 6: Full verification**

```bash
uv run --frozen ruff check src/ tests/ && uv run --frozen ruff format --check src/ tests/ && uv run --frozen mypy src/
uv run --frozen --extra dev pytest -q
```
Expected: all green (testcontainers run every migration incl. 0027 on a fresh DB, which proves 0002 → 0027 ordering works).

- [ ] **Step 7: Docs + commit**

`docs/feature_catalog.md`: delete the sentence "A legacy flow (`CHECKING`, `WAITING_FOR_TEACHER_REVIEW`, `CHECK_FAILED`, …) is retained…" and change the "ZIP only" convention to: "**ZIP only.** Submissions are ZIP uploads; the GitHub-PR / GitLab-MR ingest was retired and its enum values dropped in migration 0027."
`docs/known_bugs.md` #8: append "**Closed (2026-09-17):** the retired event types were dropped from the enum in migration 0027; the branch no longer exists."
```bash
git add -A
git commit -m "Drop legacy submission statuses and GitHub enum values (0027)

The CHECKING/WAITING_FOR_TEACHER_REVIEW flow and the GITHUB_PR/GITLAB_MR
source types have produced no rows since June. Keeping them meant every
status switch in templates and tests carried dead branches. The migration
maps any historical row to its current equivalent before recreating the
enums, so it is safe on a database whose contents we have not seen.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Reject `SHORT_ANSWER` at config-apply time (audit B6)

**Files:**
- Modify: `src/submissions_checker/services/config_apply.py` — add `_validate_quiz_questions(new_cfg)` called from `apply()` right after the `subjectCode` check (line ~113)
- Modify: `src/submissions_checker/db/models/enums.py` — delete `SHORT_ANSWER` from `QuizQuestionType`
- Modify: `src/submissions_checker/api/routes/student_quiz.py:369-371` (docstring), `:413-415` (delete the `elif q_type == "SHORT_ANSWER":` branch)
- Modify: `src/submissions_checker/services/quiz_scoring.py:104-106` (docstring)
- Modify: `tests/functional/test_student_quiz_proctoring.py:16, 693-760` (delete the SHORT_ANSWER scenario and docstring mentions)
- Modify: `docs/PLUGIN_AUTHORING.md` "Question keys" table `type` row; `docs/feature_catalog.md` §5 question-type list
- Test: `tests/functional/test_apply_config.py`

**Interfaces:**
- Produces: `ConfigApplyService.apply` raises `ValueError("assignment 'lab1' quiz question #3: type 'short_answer' is not supported (allowed: single_choice, multiple_choice, true_false, ordering)")` → surfaced by the route as `apply_error=…`.

- [ ] **Step 1: Failing functional test**

Append to `tests/functional/test_apply_config.py`:
```python
async def test_short_answer_question_is_rejected(teacher_client: AsyncClient, db: AsyncSession) -> None:
    cfg = _base_config()
    cfg["assignments"]["lab1"]["review_mode"] = "tests_then_quiz"
    cfg["assignments"]["lab1"]["quiz"] = {
        "questions": [
            {"type": "single_choice", "text": "ok?", "choices": [{"text": "y", "is_correct": True}]},
            {"type": "short_answer", "text": "explain"},
        ]
    }
    resp = await teacher_client.post(
        ENDPOINT, files={"config_zip": ("c.zip", _make_zip(cfg), "application/zip")}
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "apply_error=" in location
    assert "short_answer" in urllib.parse.unquote(location)
    assert (await db.execute(select(func.count()).select_from(Subject))).scalar_one() == 0
```
(Match the multipart field name to what the existing tests in this file use — copy from `test_apply_config_creates_subject` or similar.)
Run: `uv run --frozen --extra dev pytest tests/functional/test_apply_config.py::test_short_answer_question_is_rejected -q` → FAIL (subject created).

- [ ] **Step 2: Implement the validator**

In `config_apply.py` add a module-level constant and method:
```python
_ALLOWED_QUESTION_TYPES = ("single_choice", "multiple_choice", "true_false", "ordering")


    def _validate_quiz_questions(self, new_cfg: dict[str, Any]) -> None:
        """Reject question types the grader cannot score.

        `short_answer` used to be accepted, stored, and silently scored 0 while
        still counting toward max_score, so a quiz with one could never reach
        100 %. Refusing it at upload is the honest behaviour.
        """
        for code, a_cfg in (new_cfg.get("assignments") or {}).items():
            questions = ((a_cfg or {}).get("quiz") or {}).get("questions") or []
            for idx, q in enumerate(questions, start=1):
                q_type = str((q or {}).get("type", "")).lower()
                if q_type not in _ALLOWED_QUESTION_TYPES:
                    allowed = ", ".join(_ALLOWED_QUESTION_TYPES)
                    raise ValueError(
                        f"assignment '{code}' quiz question #{idx}: type '{q_type}' is not "
                        f"supported (allowed: {allowed})"
                    )
```
Call it in `apply()` immediately after the `subject_code` check:
```python
        self._validate_quiz_questions(new_cfg)
```

- [ ] **Step 3: Remove the runtime branch and enum member**

`enums.py`: delete `SHORT_ANSWER = "SHORT_ANSWER"`.
`student_quiz.py`: delete lines 413–415 (`elif q_type == "SHORT_ANSWER": … return {"text": text_answer}, None, 0`); rewrite the `_grade_answer` docstring sentence to: "``is_correct`` is ``None`` only for a snapshot type the grader does not know, which config-apply now prevents." Keep the `bool | None` return type (the column stays nullable).
`quiz_scoring.py`: replace the SHORT_ANSWER sentence in the `score_attempt` docstring with "so questions the student never reached still count against them."

- [ ] **Step 4: Fix tests and docs**

`tests/functional/test_student_quiz_proctoring.py`: delete the SHORT_ANSWER scenario (lines ~693–760, the test whose docstring says "SHORT_ANSWER is recorded but auto-scored 0") and the mention at line 16.
`docs/PLUGIN_AUTHORING.md` — `type` row: `single_choice`, `multiple_choice`, `true_false`, `ordering`. Remove the parenthetical about `short_answer`; add a sentence after the table: "Any other type is rejected when the config is applied."
`docs/feature_catalog.md` §5: "Question types (`QuizQuestionType`): `SINGLE_CHOICE`, `MULTIPLE_CHOICE`, `ORDERING`, `TRUE_FALSE`." Remove "(short answers are recorded but not auto-scored)".

- [ ] **Step 5: Verify**

```bash
uv run --frozen --extra dev pytest tests/functional/test_apply_config.py tests/functional/test_student_quiz_proctoring.py tests/functional/test_student_quiz.py tests/unit -q
uv run --frozen ruff check src/ tests/ && uv run --frozen mypy src/
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "Reject short_answer quiz questions at config apply

They were stored, scored 0 and still counted toward max_score, so any quiz
containing one could never reach 100 %. Nobody built the manual-grading
panel that would make them meaningful; refusing the type up front is the
honest contract.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: Seed hygiene (audit B11, scoped)

Scope decision: the seed migrations `0002` (subjects/students) and `0003` (dev accounts) are what `make e2e` and the login page's `is_development` hint rely on at stack boot, so they **stay** as migrations. `0007_quiz_dummy_data.py` seeds tables that `0013_remove_quiz_tables.py` drops on the same fresh-DB run — pure waste — and becomes a no-op.

**Files:**
- Modify: `alembic/versions/0007_quiz_dummy_data.py` — replace `upgrade()` body with `pass`; delete `downgrade()` body likewise; delete `_is_dev` and every helper; keep the revision header
- Modify: `docker-compose.yml:60` — delete `      - ./migrations:/app/migrations` (the `migrations/` dir is an empty leftover)
- Modify: `README.md` (Task 11 writes the dev-seed sentence)

- [ ] **Step 1: Neuter 0007**

Rewrite `alembic/versions/0007_quiz_dummy_data.py` to:
```python
"""Quiz dummy data — retired.

Revision ID: 0007
Revises: 0006

This migration used to seed quiz_templates/quiz_questions for development.
0013 drops those tables on the same fresh-database run, so the seed never
survived. Kept as a no-op so existing databases keep a linear history.
"""

from __future__ import annotations

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
```

- [ ] **Step 2: Drop the dead compose mount**

In `docker-compose.yml` delete the `      - ./migrations:/app/migrations` line under `app.volumes`.

- [ ] **Step 3: Verify a fresh DB still boots**

```bash
uv run --frozen --extra dev pytest tests/integration/test_migration_lock.py tests/integration/test_database.py -q
docker compose down -v && docker compose up -d && sleep 20 && curl -sf localhost:8000/health/ready
```
Expected: tests PASS; readiness 200.

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/0007_quiz_dummy_data.py docker-compose.yml
git commit -m "Make the 0007 quiz seed a no-op and drop the dead migrations mount

0013 removes the tables 0007 seeded, so on every fresh database the seed
ran and was immediately discarded. The migrations/ directory has been
empty since the Alembic rewrite.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 10: Delete deprecated docs, fix links, close known bugs, drop `missing_features.md`

**Files:**
- Delete: `docs/statuses.md`, `docs/jobs.md`, `docs/analytics.md`, `docs/missing_features.md`
- Modify: `docs/handover-checklist.md:75,101,247`, `docs/teacher_journey_guide.md:9,331,432-433`, `docs/known_bugs.md:279` (link removals), `docs/pythonbasics_task_proposals.md:109`
- Modify: `docs/known_bugs.md` — #5 → ✅ "Removed 2026-09-17", #10 → ✅ "orphan templates deleted 2026-09-17"
- Modify: `docs/feature_catalog.md` — delete §8 Analytics entirely and renumber §9 → §8; delete the `Internal user API (skeleton CRUD)` row in §1; in §4 change "Review one submission (test results, AI review, submitted code)" to "Review one submission (test results, submitted code)"; in §2 remove "no analytics link" and "no grade export button" from the note (export button lands in Plan 3 — leave the sentence "no grade export button" for now, remove only "no analytics link")
- Modify: `docs/feature_audit.md` §D — replace "Recommend deleting `missing_features.md`…" with "`missing_features.md` deleted 2026-09-17; this file is the single list."

- [ ] **Step 1: Delete and fix links**

```bash
git rm docs/statuses.md docs/jobs.md docs/analytics.md docs/missing_features.md
grep -rn 'statuses.md\|jobs.md\|analytics.md\|missing_features.md' docs/ README.md
```
For every hit outside `feature_audit.md`:
- `handover-checklist.md:75` — replace with `- [ ] Read the state machine in \`src/submissions_checker/core/state_machine.py\` and the diagram in \`docs/feature_catalog.md\` §4`
- `handover-checklist.md:101` — replace with `- [ ] Read \`src/submissions_checker/core/scheduler.py\` for the four scheduled jobs and \`workers/scheduled/outbox_processor.py\` for dispatch`
- `handover-checklist.md:247` — replace the item with `1. \`docs/feature_catalog.md\` + \`core/state_machine.py\``
- `teacher_journey_guide.md:9` — drop `, [analytics.md](analytics.md)` from the link list
- `teacher_journey_guide.md:331` — delete the sentence "Full breakdown in [analytics.md](analytics.md)."
- `teacher_journey_guide.md:432-433` — change to "GitHub-PR / Google-Forms pipeline has been retired (see git history before 2026-06-19)."
- `known_bugs.md:279` — delete the italic line pointing at `missing_features.md`
- `pythonbasics_task_proposals.md:109` — change `docs/missing_features.md` to `docs/feature_audit.md`

- [ ] **Step 2: Close bugs and fix the catalogue**

Edits exactly as listed in **Files** above. For known bug #5 change the heading to `## 5. ✅ \`/api/v1/users\` endpoints are live but fake` and append `**Removed (2026-09-17):** router deleted; the paths 404.` For #10 change 🟡 to ✅ and append `**Closed (2026-09-17):** the three orphan templates were deleted.`

- [ ] **Step 3: Verify links**

```bash
grep -rn 'statuses.md\|jobs.md\|analytics.md\|missing_features.md' docs/ README.md | grep -v feature_audit.md
```
Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add -A docs/
git commit -m "Delete docs that describe removed features and close bugs 5 and 10

statuses.md and jobs.md marked themselves historical; analytics.md
documented pages removed in e9611a2; missing_features.md is superseded by
feature_audit.md.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 11: Rewrite README

**Files:**
- Rewrite: `README.md` (496 lines → ~120)

- [ ] **Step 1: Replace the file**

Write `README.md` with this content (fill the `<…>` from `Makefile`/`docs` if a command name differs):
```markdown
# Submissions Checker

Automated checker for university programming coursework. Students upload a ZIP, the
platform runs the subject's check scripts in a locked-down Docker sandbox, and the
result flows through whatever the assignment asks for next: nothing, an AI code review,
a teacher review, or a proctored quiz. Grades, notifications, disputes and course
feedback are all in one place. UI is Ukrainian.

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
make up                         # postgres + app + localstack + prometheus + grafana
open http://localhost:8000      # dev seed (migrations 0002/0003) creates demo accounts; see login page
make logs-app
```

Subjects for local testing live in `plugins/` (gitignored). Symlink a subject repo there
and upload its config ZIP from the teacher dashboard, or use `plugins/e2e_test`.

## Tests and quality

```bash
uv run --frozen --extra dev pytest -q        # unit + integration + functional (Docker needed)
make e2e                                     # Playwright/pytest-bdd against the compose stack
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
| `docs/student_journey_guide.md`, `teacher_journey_guide.md`, `admin_journey_guide.md` | narrative walkthroughs |
| `docs/known_bugs.md`, `docs/feature_audit.md` | what is broken, what is unfinished |

## License

See `LICENSE`.
```

- [ ] **Step 2: Verify every path named in the README exists**

```bash
for p in $(grep -oP '`docs/[a-zA-Z_./-]+`|`tests/README.md`|`src/[a-zA-Z_./]+`' README.md | tr -d '`' | sort -u); do [ -e "$p" ] || echo "MISSING $p"; done
```
Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "Rewrite README to describe the product that exists

The old README documented GitHub webhooks, PR comments and SQL-file
migrations — none of which survive. The new one states the ZIP → sandbox →
review flow, how to run and test, and where the real docs are.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 12: Openspec housekeeping

**Files:**
- Delete: `openspec/changes/{fix-submission-extract-dir-permissions,generate-checks-for-python-basics,harden-anticheat-keyboard-shortcuts,notify-student-on-violation,retire-github-pr-ingest,subject-config-apply}/`
- Archive (via skill): `config-only-subject-management`, `gather-feedbacks`, `multilanguage-i18n-support`, `rebrand-and-hide-demo-credentials`, `test-mode-for-teacher`
- Leave: `prod-deployment` (4 open tasks are manual host verification — outside code)

- [ ] **Step 1: Confirm the six dirs are empty apart from stubs**

```bash
for d in fix-submission-extract-dir-permissions generate-checks-for-python-basics harden-anticheat-keyboard-shortcuts notify-student-on-violation retire-github-pr-ingest subject-config-apply; do echo "$d: $(find openspec/changes/$d -type f -not -name CLAUDE.md | wc -l)"; done
```
Expected: every count 0. Then `rm -rf` each of the six.

- [ ] **Step 2: Archive the five completed changes**

For each name, invoke the `opsx:archive` skill (`/opsx:archive <name>`) so the delta specs sync into `openspec/specs/` and the directory moves to `openspec/changes/archive/2026-09-17-<name>/`. Note for `multilanguage-i18n-support`: its spec describes the language selector removed in Task 6 — after archiving, edit the synced spec under `openspec/specs/` to state "Single language (uk). The selector and `/set-language` were removed on 2026-09-17."

- [ ] **Step 3: Verify and commit**

```bash
ls openspec/changes
```
Expected: `archive  CLAUDE.md  prod-deployment`.
```bash
git add -A openspec/
git commit -m "Archive five completed openspec changes and drop empty leftovers

All five were 100% done per tasks.md; the six removed directories held
nothing but an auto-generated stub.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 13: Root-level clutter

Everything here is untracked. Nothing is committed; this task is filesystem-only.

- [ ] **Step 1: Move the blog drafts out**

```bash
mv pendingPosts ../pendingPosts
```

- [ ] **Step 2: Delete stub-only dirs and coverage output**

```bash
rm -rf networking-backend outlines rawThoughts memory migrations services src/components cppBasicSubject pythonBasicSubject htmlcov coverage.xml
```
(`migrations/` mount was already removed from compose in Task 9.)

- [ ] **Step 3: Delete auto-generated `CLAUDE.md` stubs, keep the real one**

```bash
find . -name CLAUDE.md -not -path './.claude/CLAUDE.md' -not -path './.git/*' -size -600c -delete
find . -name CLAUDE.md -not -path './.git/*'
```
Expected: only `./.claude/CLAUDE.md` (plus any stub > 600 bytes — inspect those by hand; the pendingPosts ones are gone with the move).

- [ ] **Step 4: Verify the tree**

```bash
git status --short | head
ls
```
Expected: `git status` shows only intended tracked changes (none from this task); `ls` shows no `pendingPosts networking-backend outlines rawThoughts memory migrations services`.

---

### Task 14: Final verification and audit bookkeeping

- [ ] **Step 1: Full gates**

```bash
uv run --frozen ruff check src/ tests/
uv run --frozen ruff format --check src/ tests/
uv run --frozen mypy src/
uv run --frozen --extra dev pytest -q
```
Expected: all pass. Record the new test count.

- [ ] **Step 2: Bring the dev stack up from scratch and smoke it**

```bash
docker compose down -v && docker compose up -d --build
sleep 30 && curl -sf localhost:8000/health/ready && curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/auth/login
```
Expected: ready 200, login 200.

- [ ] **Step 3: Mark the audit**

In `docs/feature_audit.md` prefix each of A1–A9, B4, B6, B11, B12 headings with `✅ ` and add one line under §A: "All §A items, B4, B6, B11 (scoped to 0007 + compose mount) and B12 were completed 2026-09-17 in plan `docs/superpowers/plans/2026-09-17-cleanup-dead-code.md`." Under B11 note the scope decision (0002/0003 stay because e2e and the login hint depend on boot-time seeding).

- [ ] **Step 4: Update `.claude/CLAUDE.md`**

Remove the `users.py … FAKE stubs` line, the `i18n.py /set-language` line, the `user_service.py, testing/, submission_checker.py DEAD stubs` line; change `4 APScheduler jobs` line unchanged; change the docs-map row for deprecated docs to remove `statuses.md, jobs.md, analytics.md`.

- [ ] **Step 5: Commit**

```bash
git add docs/feature_audit.md
git commit -m "Mark the cleanup items done in the feature audit

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```
