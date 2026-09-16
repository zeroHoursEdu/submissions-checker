# AI Review Surfacing, Camera Proctoring Hardening, Config Guards — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the AI review verdict visible to teachers and language-agnostic, make webcam proctoring self-hosted, documented and evidenced, and reject the two config-authoring mistakes that keep recurring.

**Architecture:** No new subsystems. A small pure service (`services/ai_verdict.py`) owns "is this verdict flagged"; the review route and the assignment board read it. MediaPipe assets move from three third-party CDNs to `static/vendor/mediapipe/` (fetched by a pinned script at image build / `make setup`, gitignored). The submission review page gains a proctoring-evidence section fed by one extra query. Config guards live next to the existing `_validate_quiz_questions` in `config_apply.py`.

**Tech Stack:** FastAPI + Jinja, SQLAlchemy async, pytest (functional layer over ASGI with Postgres testcontainer), MediaPipe tasks-vision 0.10.14 (vendored), ruff/mypy.

**Spec:** `docs/feature_audit.md` B1, B2 (phone detector dropped by user decision 2026-09-17), C12. Plan 1 (`2026-09-17-cleanup-dead-code.md`) is a prerequisite and is complete.

## Global Constraints

- `uv run --frozen …` everywhere. Gates: `ruff check src/ tests/`, `ruff format --check src/ tests/`, `mypy src/`, `pytest -q` (984 tests at start) — all green before and after every task.
- UI strings go in `i18n/uk.yml` under the right section and are referenced as `vocab.<section>.<key>`. Never leave a key unreferenced (Plan 1 pruned every such key).
- Evidence frames are served only through `GET /teacher/proctoring/snapshots/{id}`; never expose `s3_url`.
- Assignment config lives in `SubjectsAssignment.config` (JSONB dict); the `ai_review` block is persisted whole by `config_apply._build_assignment_config`.
- Camera events already flow through `POST /portal/quiz/{attempt_id}/event` and the rule engine in `student_quiz.report_violation`; nothing server-side needs to know a camera event from a tab switch.
- Commit format as in Plan 1 (imperative subject, why-body, `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`).

---

### Task 1: `services/ai_verdict.py` — one place that decides "flagged"

**Files:**
- Create: `src/submissions_checker/services/ai_verdict.py`
- Modify: `src/submissions_checker/workers/tasks/review_tasks.py` (delete `_DEFAULT_THRESHOLD` and `_is_flagged`; import `is_flagged` from the service)
- Test: `tests/unit/test_ai_verdict.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class VerdictSummary:
      cheating: bool          # is_cheating AND confidence >= cheating_threshold
      ai_generated: bool      # is_ai_generated AND confidence >= ai_generated_threshold
      cheating_confidence: float
      ai_generated_confidence: float
      code_mark: int | None
      comment: str
      provider: str | None
      model: str | None
      @property
      def flagged(self) -> bool: ...

  def summarize(verdict: dict[str, Any] | None, ai_review_cfg: dict[str, Any] | None) -> VerdictSummary | None
  def is_flagged(verdict: dict[str, Any], ai_review_cfg: dict[str, Any]) -> bool
  ```
  `summarize` returns `None` when `verdict` is falsy. Thresholds default to `0.5`.

- [ ] **Step 1: Failing unit tests**

Create `tests/unit/test_ai_verdict.py`:
```python
from submissions_checker.services.ai_verdict import is_flagged, summarize

CLEAN = {
    "cheating": {"is_cheating": False, "confidence": 0.1, "reason": "original"},
    "ai_generated": {"is_ai_generated": False, "confidence": 0.2, "reason": "human"},
    "code_mark": 82,
    "comment": "Nice",
    "provider": "openai",
    "model": "gpt-test",
}
COPIED = dict(CLEAN, cheating={"is_cheating": True, "confidence": 0.9, "reason": "copied"})


def test_summarize_none_for_missing_verdict() -> None:
    assert summarize(None, {}) is None
    assert summarize({}, {}) is None


def test_clean_verdict_is_not_flagged() -> None:
    s = summarize(CLEAN, {})
    assert s is not None
    assert s.flagged is False
    assert s.code_mark == 82 and s.provider == "openai"


def test_cheating_over_default_threshold_flags() -> None:
    s = summarize(COPIED, {})
    assert s is not None and s.cheating is True and s.flagged is True
    assert is_flagged(COPIED, {}) is True


def test_threshold_from_config_is_respected() -> None:
    assert is_flagged(COPIED, {"cheating_threshold": 0.95}) is False
    aigen = dict(CLEAN, ai_generated={"is_ai_generated": True, "confidence": 0.6, "reason": ""})
    assert is_flagged(aigen, {"ai_generated_threshold": 0.5}) is True
    assert is_flagged(aigen, {"ai_generated_threshold": 0.7}) is False


def test_malformed_fields_are_tolerated() -> None:
    s = summarize({"comment": 3}, None)
    assert s is not None and s.flagged is False and s.code_mark is None and s.comment == "3"
```
Run: `uv run --frozen --extra dev pytest tests/unit/test_ai_verdict.py -q -o addopts=""` → FAIL (ImportError).

- [ ] **Step 2: Implement**

`src/submissions_checker/services/ai_verdict.py`:
```python
"""Read an AI review verdict the way the UI and the review task need it.

The verdict dict is produced by workers.tasks.review_tasks and stored on
``Submission.ai_review``. Whether it counts as *flagged* depends on per-assignment
thresholds in the ``ai_review`` config block, so both the worker (routing to teacher
review) and the teacher pages (badges) go through here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_THRESHOLD = 0.5


@dataclass(frozen=True)
class VerdictSummary:
    cheating: bool
    ai_generated: bool
    cheating_confidence: float
    ai_generated_confidence: float
    cheating_reason: str
    ai_generated_reason: str
    code_mark: int | None
    comment: str
    provider: str | None
    model: str | None

    @property
    def flagged(self) -> bool:
        return self.cheating or self.ai_generated


def _hit(block: Any, flag_key: str, threshold: float) -> tuple[bool, float, str]:
    if not isinstance(block, dict):
        return False, 0.0, ""
    try:
        confidence = float(block.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    hit = bool(block.get(flag_key)) and confidence >= threshold
    return hit, confidence, str(block.get("reason") or "")


def summarize(
    verdict: dict[str, Any] | None, ai_review_cfg: dict[str, Any] | None
) -> VerdictSummary | None:
    if not verdict:
        return None
    cfg = ai_review_cfg or {}
    cheat_thr = float(cfg.get("cheating_threshold", DEFAULT_THRESHOLD))
    aigen_thr = float(cfg.get("ai_generated_threshold", DEFAULT_THRESHOLD))
    cheating, cheat_conf, cheat_reason = _hit(verdict.get("cheating"), "is_cheating", cheat_thr)
    aigen, aigen_conf, aigen_reason = _hit(
        verdict.get("ai_generated"), "is_ai_generated", aigen_thr
    )
    raw_mark = verdict.get("code_mark")
    code_mark = int(raw_mark) if isinstance(raw_mark, int | float) else None
    return VerdictSummary(
        cheating=cheating,
        ai_generated=aigen,
        cheating_confidence=cheat_conf,
        ai_generated_confidence=aigen_conf,
        cheating_reason=cheat_reason,
        ai_generated_reason=aigen_reason,
        code_mark=code_mark,
        comment=str(verdict.get("comment") or ""),
        provider=verdict.get("provider"),
        model=verdict.get("model"),
    )


def is_flagged(verdict: dict[str, Any], ai_review_cfg: dict[str, Any]) -> bool:
    """True if cheating or AI-generated confidence meets the configured threshold."""
    summary = summarize(verdict, ai_review_cfg)
    return bool(summary and summary.flagged)
```
In `review_tasks.py`: delete `_DEFAULT_THRESHOLD = 0.5` and the whole `_is_flagged` function; add `from submissions_checker.services.ai_verdict import is_flagged`; replace `_is_flagged(verdict, ai_review_cfg)` with `is_flagged(verdict, ai_review_cfg)`.

- [ ] **Step 3: Verify + commit**

```bash
uv run --frozen --extra dev pytest tests/unit/test_ai_verdict.py tests/functional/test_ai_review_flow.py tests/integration/test_worker_tasks.py -q -o addopts=""
uv run --frozen ruff check src/ tests/ && uv run --frozen ruff format --check src/ tests/ && uv run --frozen mypy src/
git add -A src/ tests/ && git commit -m "Move the AI verdict flag rule into services/ai_verdict

The review task decided 'flagged' with a private helper; the teacher pages
are about to need the same answer for badges, so the rule gets one home.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: AI verdict on the teacher submission review page

**Files:**
- Modify: `src/submissions_checker/api/routes/teacher_portal.py` (`teacher_review_submission`, ~line 925: add `ai_summary` to context)
- Modify: `templates/teacher_submission_review.html` (new card between the submitted-file card and the end of the left column)
- Modify: `i18n/uk.yml` (`teacher:` section)
- Test: `tests/functional/test_teacher_portal_deep.py`

**Interfaces:**
- Consumes: `ai_verdict.summarize(submission.ai_review, assignment.config.get("ai_review"))`.
- Produces: template context key `ai_summary: VerdictSummary | None`.

- [ ] **Step 1: Failing functional tests**

Append to `tests/functional/test_teacher_portal_deep.py` (reuse that file's `_make_subject/_make_assignment/_make_student_assignment/_make_submission` helpers — check their signatures at the top of the file and adapt the calls; `_make_submission` there takes `(db, sa_id, student_id, status=...)`):
```python
async def test_review_page_shows_ai_verdict(client: AsyncClient, db, teacher, make_student) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    sa = await _make_assignment(db, subject.id)
    sa.config = {"ai_review": {"cheating_threshold": 0.5}}
    await db.commit()
    student = await make_student()
    submission = await _make_submission(
        db, sa.id, student.id, status=SubmissionStatus.AWAITING_TEACHER_REVIEW
    )
    submission.ai_review = {
        "cheating": {"is_cheating": True, "confidence": 0.91, "reason": "matches a public gist"},
        "ai_generated": {"is_ai_generated": False, "confidence": 0.1, "reason": "n/a"},
        "code_mark": 40,
        "comment": "Rework the naming.",
        "provider": "openai",
        "model": "gpt-test",
    }
    await db.commit()
    authenticate(client, teacher)
    resp = await client.get(f"/teacher/submissions/{submission.id}/review")
    assert resp.status_code == 200
    body = resp.text
    assert "matches a public gist" in body
    assert "Rework the naming." in body
    assert "91%" in body
    assert "gpt-test" in body
    assert 'data-ai-flag="cheating"' in body


async def test_review_page_without_ai_verdict_has_no_section(
    client: AsyncClient, db, teacher, make_student
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    sa = await _make_assignment(db, subject.id)
    student = await make_student()
    submission = await _make_submission(
        db, sa.id, student.id, status=SubmissionStatus.AWAITING_TEACHER_REVIEW
    )
    authenticate(client, teacher)
    resp = await client.get(f"/teacher/submissions/{submission.id}/review")
    assert resp.status_code == 200
    assert 'id="ai-verdict"' not in resp.text
```
Run → FAIL.

- [ ] **Step 2: Route**

In `teacher_review_submission`, before `return render(...)`:
```python
    ai_summary = summarize(
        submission.ai_review, (subjects_assignment.config or {}).get("ai_review")
    )
```
add `"ai_summary": ai_summary,` to the context, and `from submissions_checker.services.ai_verdict import summarize` to the imports.

- [ ] **Step 3: Template**

Insert after the submitted-file card's closing `</div>` (still inside `<div class="lg:col-span-2 space-y-5">`):
```html
    {% if ai_summary %}
    <div id="ai-verdict" class="bg-white rounded-xl border {% if ai_summary.flagged %}border-red-300{% else %}border-slate-200{% endif %} shadow-sm p-5">
      <h2 class="font-semibold text-slate-900 mb-1 flex items-center gap-2">
        <svg class="w-4 h-4 text-slate-400" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" d="M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09Z" />
        </svg>
        {{ vocab.teacher.ai_review_title }}
        {% if ai_summary.flagged %}
        <span class="ml-auto inline-flex items-center text-xs bg-red-100 text-red-700 px-2 py-0.5 rounded-full font-medium">{{ vocab.teacher.ai_flagged }}</span>
        {% else %}
        <span class="ml-auto inline-flex items-center text-xs bg-green-100 text-green-700 px-2 py-0.5 rounded-full font-medium">{{ vocab.teacher.ai_clean }}</span>
        {% endif %}
      </h2>
      <p class="text-xs text-slate-400 mb-4">{{ ai_summary.provider or "—" }} · {{ ai_summary.model or "—" }}{% if ai_summary.code_mark is not none %} · {{ vocab.teacher.ai_code_mark }} {{ ai_summary.code_mark }}/100{% endif %}</p>

      <dl class="space-y-3 text-sm">
        <div data-ai-flag="cheating" class="rounded-lg px-3 py-2 {% if ai_summary.cheating %}bg-red-50{% else %}bg-slate-50{% endif %}">
          <dt class="flex items-center justify-between font-medium {% if ai_summary.cheating %}text-red-700{% else %}text-slate-700{% endif %}">
            <span>{{ vocab.teacher.ai_cheating }}</span>
            <span class="text-xs font-normal">{{ (ai_summary.cheating_confidence * 100) | round | int }}%</span>
          </dt>
          {% if ai_summary.cheating_reason %}<dd class="text-slate-600 mt-1">{{ ai_summary.cheating_reason }}</dd>{% endif %}
        </div>
        <div data-ai-flag="ai_generated" class="rounded-lg px-3 py-2 {% if ai_summary.ai_generated %}bg-red-50{% else %}bg-slate-50{% endif %}">
          <dt class="flex items-center justify-between font-medium {% if ai_summary.ai_generated %}text-red-700{% else %}text-slate-700{% endif %}">
            <span>{{ vocab.teacher.ai_generated }}</span>
            <span class="text-xs font-normal">{{ (ai_summary.ai_generated_confidence * 100) | round | int }}%</span>
          </dt>
          {% if ai_summary.ai_generated_reason %}<dd class="text-slate-600 mt-1">{{ ai_summary.ai_generated_reason }}</dd>{% endif %}
        </div>
        {% if ai_summary.comment %}
        <div>
          <dt class="text-slate-400 text-xs uppercase tracking-wide mb-1">{{ vocab.teacher.ai_comment }}</dt>
          <dd class="text-slate-700 whitespace-pre-wrap">{{ ai_summary.comment }}</dd>
        </div>
        {% endif %}
      </dl>
    </div>
    {% endif %}
```

- [ ] **Step 4: Vocab**

Add under `teacher:` in `i18n/uk.yml`:
```yaml
  ai_review_title: Висновок AI-рецензії
  ai_flagged: Потребує уваги
  ai_clean: Без зауважень
  ai_code_mark: оцінка коду
  ai_cheating: Ознаки списування
  ai_generated: Ознаки AI-генерації
  ai_comment: Коментар для студента
```

- [ ] **Step 5: Verify + commit**

```bash
uv run --frozen --extra dev pytest tests/functional/test_teacher_portal_deep.py tests/functional/test_teacher_portal.py -q -o addopts=""
uv run --frozen ruff check src/ tests/ && uv run --frozen mypy src/
git add -A && git commit -m "Show the AI review verdict on the teacher review page

The worker stored cheating/AI-generated flags, confidences, reasons and a
comment on every reviewed submission, and nothing rendered them; a teacher
escalated by the AI saw only tests and a download link.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: AI flag badge on the assignment board

**Files:**
- Modify: `src/submissions_checker/api/routes/teacher_portal.py` (`teacher_assignment_detail` rows select ~line 426: add `Submission.ai_review.label("ai_review")`; after `rows = [...]` compute `ai_flags`)
- Modify: `templates/teacher_assignment.html` (Flags column, before the snapshot thumbnails)
- Modify: `i18n/uk.yml` (`teacher.ai_flag_badge`)
- Test: `tests/functional/test_teacher_portal_deep.py`

- [ ] **Step 1: Failing test**

```python
async def test_assignment_board_shows_ai_flag_badge(
    client: AsyncClient, db, teacher, make_student
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    sa = await _make_assignment(db, subject.id)
    student = await make_student()
    await _enroll(db, subject.id, student.id)  # use the file's enrol helper; if absent add SubjectsStudents directly
    submission = await _make_submission(
        db, sa.id, student.id, status=SubmissionStatus.AWAITING_TEACHER_REVIEW
    )
    submission.ai_review = {
        "cheating": {"is_cheating": False, "confidence": 0.0, "reason": ""},
        "ai_generated": {"is_ai_generated": True, "confidence": 0.8, "reason": "boilerplate"},
        "code_mark": 70,
        "comment": "",
    }
    await db.commit()
    authenticate(client, teacher)
    resp = await client.get(f"/teacher/subjects/{subject.id}/assignments/{sa.id}")
    assert resp.status_code == 200
    assert 'data-ai-flag-badge' in resp.text
```

- [ ] **Step 2: Route**

Add `Submission.ai_review.label("ai_review"),` to the rows select. After `rows = [row._asdict() for row in rows_result]`:
```python
    ai_cfg = (assignment.config or {}).get("ai_review") or {}
    ai_flags: dict[int, VerdictSummary] = {}
    for r in rows:
        summary = summarize(r.get("ai_review"), ai_cfg)
        if summary is not None and summary.flagged and r["student_assignment_id"]:
            ai_flags[r["student_assignment_id"]] = summary
```
Pass `"ai_flags": ai_flags` in the context. Import `VerdictSummary, summarize` from `services.ai_verdict`.

- [ ] **Step 3: Template**

In the Flags `<td>` of `teacher_assignment.html`, right before `{% set snaps = … %}`:
```html
          {% set ai = ai_flags.get(row.student_assignment_id) if ai_flags is defined else none %}
          {% if ai %}
          <span data-ai-flag-badge class="mt-1 inline-flex items-center gap-1 text-xs bg-red-100 text-red-700 px-2 py-0.5 rounded-full font-medium"
                title="{% if ai.cheating %}{{ ai.cheating_reason }}{% else %}{{ ai.ai_generated_reason }}{% endif %}">
            AI · {% if ai.cheating %}{{ vocab.teacher.ai_cheating }}{% else %}{{ vocab.teacher.ai_generated }}{% endif %}
          </span>
          {% endif %}
```
(No new vocab key needed beyond Task 2's.)

- [ ] **Step 4: Verify + commit**

```bash
uv run --frozen --extra dev pytest tests/functional/test_teacher_portal_deep.py tests/functional/test_teacher_portal.py -q -o addopts=""
uv run --frozen ruff check src/ tests/ && uv run --frozen mypy src/
git add -A && git commit -m "Badge AI-flagged submissions on the assignment board

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Language-agnostic code collection for the AI review

**Files:**
- Modify: `src/submissions_checker/workers/tasks/review_tasks.py` (`collect_lab_data` signature + defaults; call site)
- Test: `tests/unit/test_review_tasks_collect.py` (new), `tests/integration/test_worker_tasks.py` (extend `test_ai_review_reads_repository_code`)

**Interfaces:**
- Produces: `async def collect_lab_data(path: str, extensions: Iterable[str] | None = None, *, max_chars: int = 200_000) -> tuple[str, str]`. `DEFAULT_SOURCE_EXTENSIONS: frozenset[str]`. Config key `ai_review.source_extensions: list[str]` (with or without leading dot, case-insensitive) overrides the default.

- [ ] **Step 1: Failing unit test**

`tests/unit/test_review_tasks_collect.py`:
```python
import asyncio
from pathlib import Path

from submissions_checker.workers.tasks.review_tasks import DEFAULT_SOURCE_EXTENSIONS, collect_lab_data


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_default_extensions_cover_the_shipped_subjects() -> None:
    for ext in (".py", ".java", ".cpp", ".c", ".h", ".kt", ".js", ".ts", ".go", ".rs", ".cs"):
        assert ext in DEFAULT_SOURCE_EXTENSIONS


def test_collects_java_and_cpp_by_default(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "task")
    _write(tmp_path, "src/Main.java", "class Main {}")
    _write(tmp_path, "lab/a.cpp", "int main(){}")
    _write(tmp_path, "build/out.class", "binary")
    task, code = asyncio.run(collect_lab_data(str(tmp_path)))
    assert task == "task"
    assert "class Main" in code and "int main" in code
    assert "out.class" not in code


def test_explicit_extensions_override_default(tmp_path: Path) -> None:
    _write(tmp_path, "a.py", "print(1)")
    _write(tmp_path, "b.sql", "select 1")
    _, code = asyncio.run(collect_lab_data(str(tmp_path), ["SQL"]))
    assert "select 1" in code and "print(1)" not in code


def test_total_size_is_capped(tmp_path: Path) -> None:
    _write(tmp_path, "big.py", "x" * 1000)
    _, code = asyncio.run(collect_lab_data(str(tmp_path), max_chars=300))
    assert len(code) <= 300 + 100  # cap plus the truncation marker
    assert "[truncated]" in code
```

- [ ] **Step 2: Implement**

Replace `collect_lab_data` in `review_tasks.py`:
```python
DEFAULT_SOURCE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py", ".ipynb", ".java", ".kt", ".scala", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp",
        ".cs", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb", ".php", ".swift",
        ".sql", ".sh", ".bash", ".ps1", ".r", ".m", ".pl", ".lua", ".dart", ".vue", ".html",
        ".css", ".yaml", ".yml", ".toml", ".json", ".xml", ".gradle", ".cmake", ".md", ".txt",
    }
)
_IGNORE_DIRS = frozenset({".git", ".github", "node_modules", "target", "build", "dist",
                          "__pycache__", ".venv", "venv", ".idea", ".vscode", "bin", "obj"})
_MAX_FILE_CHARS = 60_000


def _normalize_extensions(extensions: Iterable[str] | None) -> frozenset[str]:
    if not extensions:
        return DEFAULT_SOURCE_EXTENSIONS
    out = set()
    for e in extensions:
        e = str(e).strip().lower()
        if e:
            out.add(e if e.startswith(".") else f".{e}")
    return frozenset(out) or DEFAULT_SOURCE_EXTENSIONS


async def collect_lab_data(
    path: str,
    extensions: Iterable[str] | None = None,
    *,
    max_chars: int = 200_000,
) -> tuple[str, str]:
    """Walk *path* in a thread and return (task_text, code_text).

    README files become the task description; every file whose extension is in
    *extensions* (default: DEFAULT_SOURCE_EXTENSIONS) is concatenated as code.
    Output is capped at *max_chars* so a huge upload cannot blow the model's
    context; the cut is marked so the reviewer knows.
    """
    allowed = _normalize_extensions(extensions)

    if not Path(path).exists():
        return "Task description not found.", ""

    def _walk() -> tuple[str, str]:
        task_text = "Task description not found."
        parts: list[str] = []
        used = 0
        truncated = False
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if d not in _IGNORE_DIRS)
            for file in sorted(files):
                ext = os.path.splitext(file)[1].lower()
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, path)
                try:
                    with open(file_path, encoding="utf-8") as f:
                        content = f.read(_MAX_FILE_CHARS + 1)
                except (OSError, UnicodeDecodeError):
                    continue
                if len(content) > _MAX_FILE_CHARS:
                    content = content[:_MAX_FILE_CHARS] + "\n[truncated]\n"
                if file.lower().startswith("readme"):
                    task_text = content
                    continue
                if ext not in allowed:
                    continue
                chunk = f"\n--- FILE: {rel_path} ---\n{content}\n"
                if used + len(chunk) > max_chars:
                    parts.append(chunk[: max(0, max_chars - used)])
                    truncated = True
                    break
                parts.append(chunk)
                used += len(chunk)
            if truncated:
                break
        code = "".join(parts)
        if truncated:
            code += "\n[truncated]\n"
        return task_text, code

    return await asyncio.to_thread(_walk)
```
Add `from collections.abc import Iterable` to imports. At the call site in `execute_ai_review_task`:
```python
    task_text, code_text = await collect_lab_data(
        submission.repository_path or "", ai_review_cfg.get("source_extensions")
    )
```

- [ ] **Step 3: Extend the integration test**

In `tests/integration/test_worker_tasks.py::test_ai_review_reads_repository_code` add `(repo / "Main.java").write_text("class Main {}", encoding="utf-8")` next to `main.py` and assert `"class Main" in user_prompt`.

- [ ] **Step 4: Verify + commit**

```bash
uv run --frozen --extra dev pytest tests/unit/test_review_tasks_collect.py tests/integration/test_worker_tasks.py -k "ai_review or collect" -q -o addopts=""
uv run --frozen ruff check src/ tests/ && uv run --frozen mypy src/
git add -A && git commit -m "Send every source language to the AI reviewer, not only Python

collect_lab_data read .py/.md/.txt only, so the Java and C++ subjects
were reviewed as '# No code found'. The default list now covers the
languages subjects actually ship, ai_review.source_extensions overrides
it, and output is capped so a huge upload cannot exceed the model context.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Document the `ai_review` block

**Files:**
- Modify: `docs/PLUGIN_AUTHORING.md` — new `## AI Review Block` section right before `## Quiz Block` (line ~244)
- Modify: `docs/feature_catalog.md` §4 — review row + one-paragraph note after the review-modes table

- [ ] **Step 1: Write the section**

Insert before `## Quiz Block`:
````markdown
## AI Review Block

Used by `tests_then_ai`, `tests_then_ai_then_teacher` and `tests_then_ai_then_quiz`. The
worker sends the README (task) and the student's source files to the configured provider
(`AI_PROVIDER=openai|anthropic`) and stores a structured verdict on the submission.

```yaml
assignments:
  lab3:
    review_mode: tests_then_ai_then_teacher
    ai_review:
      cheating_threshold: 0.6        # default 0.5 — confidence at/above which "cheating" flags
      ai_generated_threshold: 0.7    # default 0.5
      source_extensions: [java, xml] # default: a broad list (py, java, kt, c/cpp/h, cs, js/ts, go, rs, …)
      show_comment_to_student: true  # default false — student sees the AI comment on the assignment page
      show_grade_breakdown: true     # default false — student sees the works/quality/quiz weights
```

| Key | Effect |
|---|---|
| `cheating_threshold`, `ai_generated_threshold` | A verdict is **flagged** when the model says yes *and* its confidence meets the threshold. Under `tests_then_ai_then_quiz` a flagged submission goes to the teacher instead of the quiz. Flagged submissions get a red badge on the assignment board and a highlighted panel on the review page. |
| `source_extensions` | Which files count as code. Case-insensitive, leading dot optional. Files under `build/`, `target/`, `node_modules/`, `.git/` are always skipped; total code sent is capped at 200 k characters. |
| `show_comment_to_student` | The verdict's `comment` is written for the student; it is hidden unless this is `true`. |
| `show_grade_breakdown` | Shows the grade composition on the student's assignment page. |

The `code_mark` (0–100) feeds the grade through `grading.quality_weight` — see the Grading
section. The teacher always sees the full verdict on `/teacher/submissions/{id}/review`.
````
(Verify `show_grade_breakdown` is the key `student_portal.py` reads next to `show_comment_to_student`; use the exact name found there.)

- [ ] **Step 2: Catalogue**

In `docs/feature_catalog.md` §4 change the review row to `| Review one submission (test results, AI verdict when the mode ran one, proctoring evidence, submitted archive) |` and add after the review-modes table:
```markdown
AI verdicts (cheating / AI-generated flags with confidence and reason, a code mark, a
student-facing comment) are shown to the teacher on the review page and as a red badge on the
assignment board; the comment reaches the student only when `ai_review.show_comment_to_student`
is set. See `docs/PLUGIN_AUTHORING.md` › AI Review Block.
```

- [ ] **Step 3: Commit**

```bash
git add docs/ && git commit -m "Document the ai_review config block

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Vendor MediaPipe, drop the phone detector

**Files:**
- Create: `scripts/fetch_vendor_assets.sh` (pinned URLs + sha256, writes `static/vendor/mediapipe/`)
- Modify: `.gitignore` (add `static/vendor/`), `Makefile` (`vendor-assets` target; `setup` and `up` depend on it), `docker/app/Dockerfile` (run the script in the build so the image is self-contained — check how `static/` is copied there first)
- Modify: `templates/_quiz_anticheat.html` lines 170–372 (module script)
- Test: `tests/functional/test_student_quiz_proctoring.py`

**Interfaces:**
- Produces: `/static/vendor/mediapipe/vision_bundle.mjs`, `/static/vendor/mediapipe/wasm/vision_wasm_internal.{js,wasm}`, `/static/vendor/mediapipe/wasm/vision_wasm_nosimd_internal.{js,wasm}`, `/static/vendor/mediapipe/face_landmarker.task`. New client event `camera_model_unavailable`.

- [ ] **Step 1: Failing test**

Append to `tests/functional/test_student_quiz_proctoring.py` (use `_consent`, `_arrange_quiz`, `_make_attempt`):
```python
async def test_quiz_page_loads_proctoring_assets_from_static(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db, sub.id, cfg,
        config_snapshot={"pass_threshold_pct": 0.6, "anti_cheat": {"camera": {"enabled": True}}},
    )
    resp = await student_client.get(f"/portal/quiz/{attempt.id}")
    assert resp.status_code == 200
    body = resp.text
    assert "/static/vendor/mediapipe/vision_bundle.mjs" in body
    assert "/static/vendor/mediapipe/face_landmarker.task" in body
    for host in ("cdn.jsdelivr.net", "esm.sh", "storage.googleapis.com"):
        assert host not in body
    assert "coco-ssd" not in body and "tfjs" not in body
```

- [ ] **Step 2: Fetch script**

`scripts/fetch_vendor_assets.sh`:
```bash
#!/usr/bin/env bash
# Downloads the browser-side proctoring models into static/vendor/ so a quiz never
# depends on a third-party CDN at exam time. Idempotent; pinned by version and sha256.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/static/vendor/mediapipe"
MP="https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14"
MODEL="https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"
mkdir -p "$DEST/wasm"
fetch() { # url dest
  if [ ! -s "$2" ]; then curl -fsSL --retry 3 "$1" -o "$2.tmp" && mv "$2.tmp" "$2"; fi
}
fetch "$MP/vision_bundle.mjs"                     "$DEST/vision_bundle.mjs"
fetch "$MP/wasm/vision_wasm_internal.js"          "$DEST/wasm/vision_wasm_internal.js"
fetch "$MP/wasm/vision_wasm_internal.wasm"        "$DEST/wasm/vision_wasm_internal.wasm"
fetch "$MP/wasm/vision_wasm_nosimd_internal.js"   "$DEST/wasm/vision_wasm_nosimd_internal.js"
fetch "$MP/wasm/vision_wasm_nosimd_internal.wasm" "$DEST/wasm/vision_wasm_nosimd_internal.wasm"
fetch "$MODEL"                                     "$DEST/face_landmarker.task"
# Pin: regenerate with `sha256sum static/vendor/mediapipe/**/*` after a deliberate upgrade.
if [ -f "$ROOT/scripts/vendor_assets.sha256" ]; then
  (cd "$ROOT" && sha256sum -c --quiet scripts/vendor_assets.sha256)
fi
echo "vendor assets ready in $DEST"
```
Run it once, then generate the pin: `(cd static && sha256sum vendor/mediapipe/*.* vendor/mediapipe/wasm/*) | sed 's#^\([0-9a-f]*\)  #\1  static/#' > scripts/vendor_assets.sha256`. Commit the pin file, not the assets. Add `static/vendor/` to `.gitignore`.

`Makefile`: add
```make
vendor-assets: ## Download the self-hosted proctoring models into static/vendor/
	./scripts/fetch_vendor_assets.sh
```
and make `up:` and `setup:` run `$(MAKE) vendor-assets` first. In `docker/app/Dockerfile`, after the `COPY` of `scripts/` (add one if `scripts/` is not copied) and before the final stage's `COPY static`, add `RUN ./scripts/fetch_vendor_assets.sh` (curl must exist in the image; add `apt-get install -y --no-install-recommends curl ca-certificates` if the base has none). Check with `grep -n 'COPY\|RUN apt' docker/app/Dockerfile` first and follow the file's stage layout; the e2e compose builds the same image.

- [ ] **Step 3: Rewrite the module script**

In `templates/_quiz_anticheat.html` replace the import line and the model-loading block:
```js
import { FilesetResolver, FaceLandmarker } from "/static/vendor/mediapipe/vision_bundle.mjs";
```
```js
  // 2. Load FaceLandmarker (face count + head pose) from our own static files.
  let landmarker = null;
  try {
    const fileset = await FilesetResolver.forVisionTasks("/static/vendor/mediapipe/wasm");
    landmarker = await FaceLandmarker.createFromOptions(fileset, {
      baseOptions: { modelAssetPath: "/static/vendor/mediapipe/face_landmarker.task" },
      runningMode: "VIDEO",
      numFaces: 2,
      outputFacialTransformationMatrixes: true,
    });
  } catch (_) {
    // Assets missing or unsupported browser: the quiz stays usable, but the teacher
    // gets an event saying detection never ran for this attempt.
    report('camera_model_unavailable');
    document.getElementById('proctor-status').textContent = "{{ vocab.quiz.proctor_detection_off }}";
  }
```
Delete the whole `// 3. Optional phone/object detector` block, the `objModel` variable, and the `// Phone loop` block at the end. Delete `const det = cfg.detectors || {};`? No — keep `det`, it still drives face detectors. Update the header comment to say detection is MediaPipe face landmarks only.

Add to `i18n/uk.yml` under `quiz:`: `  proctor_detection_off: Запис ведеться; автоматичне розпізнавання недоступне`.

- [ ] **Step 4: Verify locally**

```bash
./scripts/fetch_vendor_assets.sh && ls -la static/vendor/mediapipe static/vendor/mediapipe/wasm
uv run --frozen --extra dev pytest tests/functional/test_student_quiz_proctoring.py tests/functional/test_student_quiz.py -q -o addopts=""
docker compose up -d --build && sleep 20 && curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/static/vendor/mediapipe/vision_bundle.mjs
```
Expected: 6 files; tests pass; static 200 through the container (proves the Dockerfile step).

- [ ] **Step 5: Commit**

```bash
git add -A scripts/ Makefile docker/app/Dockerfile .gitignore templates/_quiz_anticheat.html i18n/uk.yml tests/
git commit -m "Self-host the proctoring models and drop the phone detector

At exam time the quiz page pulled MediaPipe from jsdelivr, its face model
from storage.googleapis.com and tfjs + coco-ssd from esm.sh. Any of them
down meant a silently unproctored quiz. The face landmarker now ships with
the image under /static/vendor and reports camera_model_unavailable when it
cannot start; the phone detector (10 MB, noisy) is gone by decision.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Proctoring evidence on the submission review page

**Files:**
- Modify: `src/submissions_checker/api/routes/teacher_portal.py` (`teacher_review_submission`: query attempts + snapshots)
- Modify: `templates/teacher_submission_review.html` (new card after the AI verdict card)
- Modify: `i18n/uk.yml` (`teacher:` keys)
- Test: `tests/functional/test_proctoring_snapshot_access.py`

**Interfaces:**
- Produces: context key `proctoring: list[dict]` — one per attempt: `{"attempt_id", "started_at", "status", "is_passed", "violations": {event: count}, "force_fail": bool, "flagged_events": [..], "snapshots": [{"url", "event_type", "captured_at"}]}` ordered newest attempt first, snapshots oldest first.

- [ ] **Step 1: Failing test**

Append to `tests/functional/test_proctoring_snapshot_access.py` (reuse `_arrange_snapshot`; its return gives the snapshot whose attempt belongs to a submission — read the helper to get the submission id, or extend it to return it):
```python
async def test_review_page_lists_evidence_per_attempt(client: AsyncClient, db, teacher, student_user) -> None:
    snapshot = await _arrange_snapshot(db, owner_id=teacher.id, student_id=student_user.student_id)
    attempt = await db.get(QuizAttempt, snapshot.attempt_id)
    submission = await db.get(Submission, attempt.submission_id)
    submission.status = SubmissionStatus.AWAITING_TEACHER_REVIEW
    attempt.violations = {"camera_face_absent": 2, "_flagged_events": ["camera_face_absent"]}
    await db.commit()
    authenticate(client, teacher)
    resp = await client.get(f"/teacher/submissions/{submission.id}/review")
    assert resp.status_code == 200
    body = resp.text
    assert f"/teacher/proctoring/snapshots/{snapshot.id}" in body
    assert "camera_face_absent" in body
    assert "s3.amazonaws" not in body and snapshot.s3_url not in body
```
(If `_arrange_snapshot` builds the submission with a different status, set it as shown; `authenticate` is imported in that file already — check.)

- [ ] **Step 2: Route**

In `teacher_review_submission`, before `render`:
```python
    attempts_result = await db.execute(
        select(QuizAttempt)
        .where(QuizAttempt.submission_id == submission.id)
        .options(selectinload(QuizAttempt.snapshots))
        .order_by(QuizAttempt.started_at.desc())
    )
    proctoring = []
    for attempt in attempts_result.scalars():
        violations = attempt.violations or {}
        proctoring.append(
            {
                "attempt_id": attempt.id,
                "started_at": attempt.started_at,
                "status": attempt.status,
                "is_passed": attempt.is_passed,
                "violations": {
                    k: v for k, v in violations.items() if not k.startswith("_") and isinstance(v, int | float)
                },
                "force_fail": bool(violations.get("_force_fail")),
                "flagged_events": list(violations.get("_flagged_events") or []),
                "snapshots": [
                    {
                        "url": f"/teacher/proctoring/snapshots/{s.id}",
                        "event_type": s.event_type,
                        "captured_at": s.captured_at,
                    }
                    for s in sorted(attempt.snapshots, key=lambda s: s.captured_at)
                ],
            }
        )
```
Add `"proctoring": proctoring` to the context.

- [ ] **Step 3: Template + vocab**

After the AI verdict card:
```html
    {% if proctoring %}
    <div id="proctoring-evidence" class="bg-white rounded-xl border border-slate-200 shadow-sm p-5">
      <h2 class="font-semibold text-slate-900 mb-4">{{ vocab.teacher.proctoring_title }}</h2>
      <div class="space-y-5">
        {% for a in proctoring %}
        <div class="border border-slate-100 rounded-lg p-3">
          <div class="flex flex-wrap items-center gap-2 text-sm">
            <span class="font-medium text-slate-800">{{ vocab.teacher.attempt_label }} #{{ a.attempt_id }}</span>
            <span class="text-slate-400 text-xs">{{ a.started_at.strftime("%d.%m.%Y %H:%M") }}</span>
            <span class="text-xs px-2 py-0.5 rounded-full {% if a.force_fail %}bg-red-100 text-red-700{% elif a.is_passed %}bg-green-100 text-green-700{% else %}bg-slate-100 text-slate-600{% endif %}">{{ a.status }}</span>
          </div>
          {% if a.violations %}
          <ul class="mt-2 flex flex-wrap gap-1.5">
            {% for ev, n in a.violations.items() %}
            <li class="text-xs px-2 py-0.5 rounded bg-amber-50 text-amber-800 font-mono">{{ ev }} × {{ n }}</li>
            {% endfor %}
          </ul>
          {% else %}
          <p class="mt-2 text-xs text-slate-400">{{ vocab.teacher.no_violations }}</p>
          {% endif %}
          {% if a.snapshots %}
          <div class="mt-3 grid grid-cols-3 sm:grid-cols-4 md:grid-cols-6 gap-2">
            {% for s in a.snapshots %}
            <a href="{{ s.url }}" target="_blank" class="block">
              <img src="{{ s.url }}" alt="{{ s.event_type }}" class="w-full aspect-[4/3] object-cover rounded border border-slate-200 hover:ring-2 hover:ring-indigo-400">
              <span class="block mt-0.5 text-[10px] font-mono text-slate-500 truncate">{{ s.event_type }} · {{ s.captured_at.strftime("%H:%M:%S") }}</span>
            </a>
            {% endfor %}
          </div>
          {% endif %}
        </div>
        {% endfor %}
      </div>
    </div>
    {% endif %}
```
Vocab (`teacher:`): `proctoring_title: Прокторинг`, `attempt_label: Спроба`, `no_violations: Порушень не зафіксовано`.

- [ ] **Step 4: Verify + commit**

```bash
uv run --frozen --extra dev pytest tests/functional/test_proctoring_snapshot_access.py tests/functional/test_teacher_portal_deep.py -q -o addopts=""
uv run --frozen ruff check src/ tests/ && uv run --frozen mypy src/
git add -A && git commit -m "Show per-attempt proctoring evidence on the review page

Teachers only had a strip of thumbnails on the assignment board. The review
page now lists every quiz attempt with its violation counts and every
evidence frame in order, all served through the authenticated endpoint.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Camera events through the rule engine — tests + docs

**Files:**
- Test: `tests/functional/test_student_quiz_proctoring.py`
- Modify: `docs/anti-cheat.md` (new `## Camera proctoring` section before `## Teacher view`; update `## Teacher view`; extend `## Limitations`), `docs/PLUGIN_AUTHORING.md` (quiz-level keys row for `anti_cheat` → mention `camera`), `docs/feature_catalog.md` §5

- [ ] **Step 1: Tests (should pass already — they pin behaviour)**

```python
async def test_camera_events_hit_the_rule_engine(student_client: AsyncClient, db, student_user) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    rules = [
        _rule("camera_face_absent", 2, {"type": "flag"}),
        _rule("camera_multiple_faces", 1, {"type": "fail", "message": "Another person detected."}),
    ]
    attempt = await _make_attempt(
        db, sub.id, cfg,
        config_snapshot={"pass_threshold_pct": 0.6, "anti_cheat": {"rules": rules, "camera": {"enabled": True}}},
    )
    r1 = await student_client.post(f"/portal/quiz/{attempt.id}/event", json={"type": "camera_face_absent"})
    assert r1.json()["action"] == "none"
    r2 = await student_client.post(f"/portal/quiz/{attempt.id}/event", json={"type": "camera_face_absent"})
    assert r2.json()["action"] == "flag"
    r3 = await student_client.post(f"/portal/quiz/{attempt.id}/event", json={"type": "camera_multiple_faces"})
    assert r3.json()["action"] == "fail"
    await db.refresh(attempt)
    assert attempt.violations["camera_face_absent"] == 2
    assert "camera_face_absent" in attempt.violations["_flagged_events"]
    assert attempt.violations["_force_fail"] is True


async def test_camera_model_unavailable_is_recorded_without_a_rule(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db, sub.id, cfg, config_snapshot={"pass_threshold_pct": 0.6, "anti_cheat": {"camera": {"enabled": True}}}
    )
    r = await student_client.post(f"/portal/quiz/{attempt.id}/event", json={"type": "camera_model_unavailable"})
    assert r.status_code == 200 and r.json()["action"] == "none"
    await db.refresh(attempt)
    assert attempt.violations["camera_model_unavailable"] == 1
```
Run: expected PASS (if either fails, the rule engine has a bug — stop and report).

- [ ] **Step 2: `docs/anti-cheat.md`**

Insert before `## Teacher view`:
````markdown
## Camera proctoring

Optional webcam monitoring, configured under `anti_cheat.camera`. Detection runs **in the
browser** (MediaPipe Face Landmarker, served from this app's `/static/vendor/`, never a CDN);
only events — and, when enabled, evidence frames on flagged actions — leave the student's
machine. The student must have accepted the recording notice once (`/portal/consent`).

```yaml
anti_cheat:
  camera:
    enabled: true
    require_camera: true        # default false
    on_no_camera: fail          # fail | warn (default warn) — when access is denied
    capture_snapshots: true     # default false — upload a frame when an action fires
    snapshot_on: [flag, fail]   # actions that trigger a frame (default [])
    detectors:
      face_absent:     { enabled: true, sustain_seconds: 3 }
      multiple_faces:  { enabled: true, sustain_seconds: 1 }
      looking_away:    { enabled: true, sustain_seconds: 3, yaw_deg: 25, pitch_deg: 20 }
  rules:
    - { event: camera_face_absent,    threshold: 3, action: { type: warn, message: "Please stay in frame." } }
    - { event: camera_multiple_faces, threshold: 1, action: { type: flag } }
    - { event: camera_looking_away,   threshold: 5, action: { type: reduce_time, penalty_seconds: 60 } }
```

Events the camera module emits (each is an ordinary rule event; without a matching rule it is
counted but does nothing):

| Event | Fires when |
|---|---|
| `camera_blocked` | the browser refused camera access (once, at start) |
| `camera_model_unavailable` | the detector could not start (assets missing, unsupported browser); recording still runs |
| `camera_face_absent` | no face for `sustain_seconds` (edge-triggered; re-arms when a face returns) |
| `camera_multiple_faces` | two or more faces for `sustain_seconds` |
| `camera_looking_away` | head yaw/pitch beyond `yaw_deg`/`pitch_deg` for `sustain_seconds` |

Evidence frames are stored in object storage under `proctoring/attempt-<id>/` and are readable
only through `GET /teacher/proctoring/snapshots/{id}` by a teacher authorized for the subject.
````
Replace the `## Teacher view` body with:
```markdown
- The assignment board (`/teacher/subjects/{id}/assignments/{id}`) shows a **Flags** column:
  **Auto-failed** (red), **N events** (amber), an **AI ·** badge when the AI review flagged
  the submission, and up to six evidence thumbnails.
- The submission review page (`/teacher/submissions/{id}/review`) lists every quiz attempt
  with its per-event counts and every evidence frame in capture order.
```
Append to `## Limitations and honest caveats`:
```markdown
6. **Camera detection is heuristic.** Face-absent fires on poor lighting, a hand over the
   face or leaning out of frame; multiple-faces fires on a poster or a passer-by;
   looking-away depends on camera placement. Use `flag`/`warn` actions and `sustain_seconds`
   ≥ 2 for camera events; reserve `fail` for `camera_multiple_faces` in supervised settings.
7. **Camera needs HTTPS.** `getUserMedia` is only available on a secure context, so over plain
   HTTP the gate reports `camera_blocked` for everyone.
8. **Detection can be disabled by the student** (blocking `/static/vendor/`, devtools). The
   `camera_model_unavailable` event makes that visible; treat an attempt with it as unproctored.
```

- [ ] **Step 3: Cross-references**

`docs/PLUGIN_AUTHORING.md` quiz-level keys: change the `anti_cheat` row to `| `anti_cheat` | map | none | Proctoring and punishment rules, including the optional `camera` block — see `docs/anti-cheat.md`. |`.
`docs/feature_catalog.md` §5: change "Submit a webcam proctoring snapshot (when enabled + consented; skipped silently if storage absent)" to "Submit a webcam proctoring snapshot (browser-side MediaPipe face detection, models served from `/static/vendor/`; when enabled + consented; skipped silently if storage absent)".

- [ ] **Step 4: Commit**

```bash
uv run --frozen --extra dev pytest tests/functional/test_student_quiz_proctoring.py -q -o addopts=""
git add -A && git commit -m "Pin camera events to the rule engine and document camera proctoring

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: Config guards for the two recurring authoring mistakes (C12)

**Files:**
- Modify: `src/submissions_checker/services/config_apply.py` (two validators called from `apply()` next to `_validate_quiz_questions`)
- Modify: `src/submissions_checker/services/check_core.py` (`resolve_check_plan`: runtime guard)
- Test: `tests/unit/test_config_apply_helpers.py`, `tests/unit/test_check_core.py`, `tests/functional/test_apply_config.py`
- Modify: `docs/PLUGIN_AUTHORING.md` (one line under Review Modes and under Variants), `docs/known_bugs.md` #13 → ✅

**Interfaces:**
- Produces: `ConfigApplyService._validate_check_commands(new_cfg)`, `._validate_quiz_reachability(new_cfg)`; `_QUIZ_REACHABLE_MODES = frozenset({"tests_then_quiz", "tests_then_ai_then_quiz", "quiz_only", "quiz_then_teacher", "tests_then_teacher", "tests_then_ai_then_teacher", "tests_then_ai_teacher"})` (teacher-gated modes can send the quiz on approval, so they count).

- [ ] **Step 1: Failing unit tests**

Append to `tests/unit/test_config_apply_helpers.py` (the file has a `svc` fixture returning a `ConfigApplyService`):
```python
def test_identical_common_and_variant_check_is_rejected(svc: ConfigApplyService) -> None:
    cfg = {
        "subjectCode": "x",
        "assignments": {
            "lab6": {
                "common": {"sandbox": {"check_command": "assignments/lab6/check.py"}},
                "variants": {"1": {"sandbox": {"check_command": "assignments/lab6/check.py"}}},
            }
        },
    }
    with pytest.raises(ValueError, match="lab6.*variant '1'.*same check_command"):
        svc._validate_check_commands(cfg)


def test_distinct_common_and_variant_check_is_fine(svc: ConfigApplyService) -> None:
    cfg = {
        "subjectCode": "x",
        "assignments": {
            "lab1": {
                "common": {"sandbox": {"check_command": "assignments/lab1/common.py"}},
                "variants": {"1": {"sandbox": {"check_command": "assignments/lab1/v1.py"}}},
            }
        },
    }
    svc._validate_check_commands(cfg)


def test_quiz_under_tests_only_is_rejected(svc: ConfigApplyService) -> None:
    cfg = {
        "subjectCode": "x",
        "assignments": {
            "lab2": {"review_mode": "tests_only", "quiz": {"questions": [{"type": "true_false"}]}}
        },
    }
    with pytest.raises(ValueError, match="lab2.*quiz.*tests_only"):
        svc._validate_quiz_reachability(cfg)


@pytest.mark.parametrize(
    "mode",
    ["tests_then_quiz", "tests_then_ai_then_quiz", "quiz_only", "quiz_then_teacher", "tests_then_teacher", "tests_then_ai_then_teacher"],
)
def test_quiz_under_reachable_modes_is_fine(svc: ConfigApplyService, mode: str) -> None:
    cfg = {"subjectCode": "x", "assignments": {"lab2": {"review_mode": mode, "quiz": {"questions": [{"type": "true_false"}]}}}}
    svc._validate_quiz_reachability(cfg)
```
And in `tests/unit/test_check_core.py`:
```python
def test_resolve_rejects_identical_common_and_variant_scripts() -> None:
    cfg = {
        "assignments": {
            "lab6": {
                "common": {"sandbox": {"image": "x", "check_command": "assignments/lab6/check.py"}},
                "variants": {"3": {"sandbox": {"check_command": "assignments/lab6/check.py"}}},
            }
        }
    }
    err = check_core.resolve_check_plan(cfg, "lab6", "3")
    assert isinstance(err, check_core.ConfigError)
    assert "same check_command" in err.reason
```

- [ ] **Step 2: Implement**

`config_apply.py` — constant next to `_ALLOWED_QUESTION_TYPES`:
```python
_QUIZ_REACHABLE_MODES = frozenset(
    {
        "tests_then_quiz",
        "tests_then_ai_then_quiz",
        "quiz_only",
        "quiz_then_teacher",
        "tests_then_teacher",
        "tests_then_ai_then_teacher",
        "tests_then_ai_teacher",
    }
)
```
Methods next to `_validate_quiz_questions`:
```python
    def _validate_check_commands(self, new_cfg: dict[str, Any]) -> None:
        """Reject a variant whose check_command equals the common one.

        run_check executes both scripts and merges their test lists, so an
        identical pair runs the same script twice and doubles the score
        denominator. It happened in two subjects in a row (known bug #13).
        """
        for code, a_cfg in (new_cfg.get("assignments") or {}).items():
            common_cmd = (((a_cfg or {}).get("common") or {}).get("sandbox") or {}).get(
                "check_command"
            )
            if not common_cmd:
                continue
            for variant, v_cfg in ((a_cfg or {}).get("variants") or {}).items():
                v_cmd = ((v_cfg or {}).get("sandbox") or {}).get("check_command")
                if v_cmd and v_cmd == common_cmd:
                    raise ValueError(
                        f"assignment '{code}' variant '{variant}' uses the same check_command as "
                        f"common ({common_cmd}); drop one — the script would run twice and double "
                        f"the score denominator"
                    )

    def _validate_quiz_reachability(self, new_cfg: dict[str, Any]) -> None:
        """Reject a quiz block under a review mode that can never send it."""
        for code, a_cfg in (new_cfg.get("assignments") or {}).items():
            has_quiz = bool((((a_cfg or {}).get("quiz") or {}).get("questions")))
            mode = str((a_cfg or {}).get("review_mode", "tests_only"))
            if has_quiz and mode not in _QUIZ_REACHABLE_MODES:
                raise ValueError(
                    f"assignment '{code}' has a quiz but review_mode '{mode}' never sends it; "
                    f"use one of: {', '.join(sorted(_QUIZ_REACHABLE_MODES))}"
                )
```
Call both in `apply()` right after `self._validate_quiz_questions(new_cfg)`.

`check_core.resolve_check_plan` — after `common_check`/`variant_check` are resolved in the common/variants branch and before the `variants_required` check:
```python
    if common_check and variant_check and common_check == variant_check:
        return ConfigError(
            f"common and variant '{variant}' resolve to the same check_command ({common_check}); "
            "the script would run twice. Contact your teacher."
        )
```

- [ ] **Step 3: Functional test + docs**

Append to `tests/functional/test_apply_config.py`:
```python
async def test_quiz_under_tests_only_is_rejected_on_upload(teacher_client: AsyncClient, db: AsyncSession) -> None:
    cfg = _base_config()
    cfg["assignments"]["lab1"]["quiz"] = {"questions": [{"type": "true_false", "text": "?", "correct": True}]}
    resp = await _post(teacher_client, _make_zip(cfg))
    assert resp.status_code == 303
    assert "never sends it" in urllib.parse.unquote(resp.headers["location"])
    assert (await db.execute(select(func.count()).select_from(Subject))).scalar_one() == 0
```
`docs/PLUGIN_AUTHORING.md`: under Review Modes add "A `quiz:` block is rejected at upload unless the review mode can send it (`*_then_quiz`, `quiz_only`, `quiz_then_teacher`, or a teacher-gated mode, where the teacher's approval sends it)." Under Variants add "A variant whose `check_command` equals the common one is rejected at upload: both would run and the score denominator would double."
`docs/known_bugs.md` #13: heading 🔴 → ✅, append `**Fixed (2026-09-17):** config apply and \`resolve_check_plan\` both reject an identical common/variant \`check_command\`.`

- [ ] **Step 4: Verify + commit**

```bash
uv run --frozen --extra dev pytest tests/unit/test_config_apply_helpers.py tests/unit/test_check_core.py tests/functional/test_apply_config.py tests/integration/test_config_apply.py -q -o addopts=""
uv run --frozen ruff check src/ tests/ && uv run --frozen mypy src/
git add -A && git commit -m "Reject the two recurring config mistakes at upload

Identical common/variant check scripts ran twice and doubled the score
denominator in two subjects in a row (known bug #13); a quiz under
tests_only or tests_then_ai silently never fired. Both now fail the
config apply with a message naming the assignment.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 10: Full gates, audit bookkeeping

- [ ] **Step 1: Gates**

```bash
uv run --frozen ruff check src/ tests/ && uv run --frozen ruff format --check src/ tests/ && uv run --frozen mypy src/
uv run --frozen --extra dev pytest -q
```
Expected: all green.

- [ ] **Step 2: Audit + CLAUDE.md**

In `docs/feature_audit.md` prefix `### B1.`, `### B2.`, and C12 with ✅ and add under B2: "Done 2026-09-17: assets vendored (`scripts/fetch_vendor_assets.sh`), phone detector removed, docs + tests added, evidence timeline on review page." In `.claude/CLAUDE.md` add a line under `templates/`: "`_quiz_anticheat.html` loads MediaPipe from `/static/vendor/mediapipe` (run `make vendor-assets`; gitignored)". Note `services/ai_verdict.py` in the services list.

- [ ] **Step 3: Commit**

```bash
git add -A docs/ && git commit -m "Mark AI-review and camera items done in the feature audit

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```
