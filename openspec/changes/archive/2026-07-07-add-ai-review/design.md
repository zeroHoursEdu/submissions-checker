## Context

The submission pipeline already has AI-review scaffolding wired end-to-end but incomplete:

- **State machine** (`core/state_machine.py`) — `TESTING` fans out to `AWAITING_AI_REVIEW → AI_REVIEWING → (AWAITING_TEACHER_REVIEW | COMPLETED | AI_REVIEW_FAILED)`. There is **no** `AI_REVIEWING → QUIZ_SENT` edge yet.
- **Producer** (`workers/tasks/check_tasks.py::_advance_after_tests`) — branches on `review_mode`: `tests_only`, `tests_then_ai`, `tests_then_teacher`, `tests_then_ai_then_teacher`, `tests_then_quiz`. No AI-then-quiz mode.
- **Handler** (`workers/tasks/review_tasks.py::execute_ai_review_task`) — OpenAI-only, produces a freeform `{"review": "..."}` blob into `submissions.ai_review`, routes to teacher/completed. No cheating/AI-gen verdict, no code mark, no Claude, no quiz gating.
- **Dead code** — `services/ai/client.py` and `services/ai/code_reviewer.py` are `NotImplementedError` skeletons, imported nowhere.
- **Grade** — `StudentAssignment.grade` is **never written anywhere** in the codebase. Per-submission test score lives on `submissions.test_results` (`{passed, score, max_score, tests}`); quiz score lives on `QuizAttempt.score/max_score`. Nothing aggregates them.
- **Config** — per-assignment config is an untyped dict stored on `SubjectPluginConfig.config` (raw YAML) and a copied subset on `subjects_assignments.config`. The copied subset is a fixed allow-list in `config_apply.py` (`_build_assignment_config`, line ~599, and the diff list ~342): `review_mode, late_policy, max_submissions, download_links, variants_required, sandbox, variants`. `quiz` is deliberately not copied (read from the raw blob).
- **Settings** — `ai_provider: str = "openai"`, `openai_api_key`, `openai_model="gpt-4"`, `ai_max_tokens=4000`. `openai>=1.54.0` is a dependency; no `anthropic`.
- **Student portal** — `assignment_detail` route + `AssignmentDetail` DTO + `templates/assignment_detail.html` render grade/status/quiz but never read `submissions.ai_review`.

Anthropic model IDs and SDK usage confirmed via the `claude-api` skill: default model `claude-opus-4-8`, structured JSON via `output_config={"format": {"type": "json_schema", "schema": ...}}`, no `temperature`/`budget_tokens` on Opus 4.8.

## Goals / Non-Goals

**Goals:**
- Add a `tests_then_ai_then_quiz` review mode: tests → AI review → (clean: quiz | flagged: teacher).
- Make the AI review produce a structured verdict (cheating, AI-generated, code mark 0–100, comment).
- Support Claude and OpenAI behind one abstraction, single active provider chosen by app config.
- Compute and persist a configurable weighted grade (code vs quiz; works vs quality) at every completion point — closing the never-written-`grade` gap.
- Two student-visibility toggles: AI comment, grade breakdown.
- Persist/diff the new config blocks through config-apply.

**Non-Goals:**
- No multi-provider-at-once, no per-assignment provider/model/key override (app-global provider only, as requested).
- No change to how the quiz itself is generated, graded, or proctored.
- No teacher UI for viewing the AI verdict beyond what already exists (existing teacher review flow is reused for escalation).
- No re-run/override of an AI verdict from the UI; retries use the existing outbox retry policy.
- No streaming; single request/response per review (submissions are small).

## Decisions

### Provider abstraction
New `services/ai/provider.py` replacing the dead skeletons:
- A small protocol `AIProvider` with `async def review(system: str, user: str, schema: dict) -> dict` that returns parsed JSON matching `schema`.
- `OpenAIProvider` — `AsyncOpenAI(api_key=settings.openai_api_key)`, `chat.completions.create(model=settings.openai_model, response_format={"type": "json_object"}, ...)`, parse `choices[0].message.content`.
- `AnthropicProvider` — `AsyncAnthropic(api_key=settings.anthropic_api_key)`, `messages.create(model=settings.anthropic_model, max_tokens=settings.ai_max_tokens, system=..., messages=[{"role":"user","content":user}], output_config={"format": {"type":"json_schema","schema":schema}})`, parse the first text block as JSON. No `temperature` (removed on Opus 4.8).
- Factory `get_ai_provider(settings)` returns the instance for `settings.ai_provider`; raises if the active provider's key is unset.
- `review_tasks.py` builds one prompt asking for the structured verdict and calls the active provider; the freeform OpenAI call is removed.

`core/config.py`: `ai_provider: Literal["openai", "anthropic"] = "openai"`, add `anthropic_api_key: str | None`, `anthropic_model: str = "claude-opus-4-8"`. Add `anthropic>=0.40` to `pyproject.toml`.

### Verdict schema (persisted to `submissions.ai_review`)
```json
{
  "cheating": {"is_cheating": bool, "confidence": float, "reason": str},
  "ai_generated": {"is_ai_generated": bool, "confidence": float, "reason": str},
  "code_mark": int,          // 0..100 quality
  "comment": str,            // student-facing
  "provider": str, "model": str
}
```
Parse failure → transition `ai_review_failed` (existing edge) and raise so the outbox retries.

### State machine + producer
- Add edge `AI_REVIEWING: {"ai_review_passed_quiz": QUIZ_SENT}` to `_TRANSITIONS`. No DB enum change (QUIZ_SENT already exists).
- `_advance_after_tests`: new branch `review_mode == "tests_then_ai_then_quiz"` → `transition(test_passed_ai)` + enqueue `RUN_AI_REVIEW` with `payload={"submission_id":..., "next_step":"quiz"}`.
- `execute_ai_review_task`: after storing the verdict, if `next_step == "quiz"`: flagged (either confidence ≥ its threshold) → `ai_review_done_teacher` + `enqueue_teacher_review_notification`; clean → `ai_review_passed_quiz`. Existing `next_step` values (`teacher`, default `completed`) keep their current behavior. Thresholds read from the assignment's `ai_review` config (defaults 0.5).

### Grading service
New `services/grading.py`, DB-free pure function plus a persistence helper:
- `compute_grade(grading_cfg, min_grade, max_grade, *, works_pct, ai_mark, quiz_pct) -> GradeBreakdown` — `works_pct`/`ai_mark`/`quiz_pct` are `float | None` (None = component absent → weight dropped and remaining renormalized). Returns final grade + component scores + effective weights.
- `finalize_grade(db, submission)` — pulls `works_pct` from `submission.test_results`, `ai_mark` from `submission.ai_review.code_mark`, `quiz_pct` from the submission's passed `QuizAttempt` (if any), computes, writes `StudentAssignment.grade` and `submission.grade_breakdown`.
- Call sites: `student_quiz.py::_grade_and_finalize` (right where it sets `COMPLETED`), and the non-quiz completion points — `_advance_after_tests` tests-only branch and `execute_ai_review_task`'s `ai_review_done_completed` path. (Teacher-approve → COMPLETED also calls it, so teacher-escalated submissions still get a grade.)

### DB
One Alembic migration: `ALTER TABLE submissions ADD COLUMN grade_breakdown JSONB NULL`. `ai_review` JSONB already exists.

### Config apply
Add `ai_review` and `grading` to the two key lists in `config_apply.py` (`_build_assignment_config` allow-list + the diff `config_keys` list). Pure additive; existing diff logic then persists and compares them.

### Student portal
- `AssignmentDetail` DTO gains `ai_comment: str | None` and `grade_breakdown: dict | None`, populated in `assignment_detail` only when the assignment's `ai_review.show_comment_to_student` / `grading.show_breakdown_to_student` toggles are true (read from the assignment config).
- `assignment_detail.html` renders the comment block and a breakdown block behind those DTO fields. Verdicts/confidences are never passed to the DTO.

### Config shape (documented for teachers; consumed as dict)
```yaml
assignments:
  lab1:
    review_mode: tests_then_ai_then_quiz
    ai_review:
      cheating_threshold: 0.6
      ai_generated_threshold: 0.6
      show_comment_to_student: true
    grading:
      code_weight: 0.6
      quiz_weight: 0.4
      code: { works_weight: 0.7, quality_weight: 0.3 }
      show_breakdown_to_student: true
```

## Risks / Trade-offs

- **AI reliability / cost.** LLM verdicts are non-deterministic and can be wrong. Mitigation: flagged work escalates to a **human teacher** (never auto-fails), and the `code_mark` only ever *contributes* to a grade by its configured weight (0 by default). Malformed output fails safe into the existing retry/`AI_REVIEW_FAILED` path.
- **Prompt-injection in student code.** Student files could contain "ignore previous instructions." Mitigation: student code is passed as user content, clearly delimited; the system prompt states the code is untrusted input to be analyzed, not instructions to follow. Verdicts are advisory + human-gated.
- **Grade weight config errors.** Teachers can set weights that sum oddly. Mitigation: weights are normalized (not required to sum to 1), and absent components renormalize — so partial/misconfigured weights still yield a sane grade rather than an error.
- **Backfill.** `grade_breakdown` is nullable and grades are only computed going forward; historical submissions keep `grade = NULL` (unchanged from today, where it was always NULL).
- **Provider default.** Defaulting `anthropic_model` to `claude-opus-4-8` is a cost choice; teachers/operators can point it at a cheaper model (`claude-haiku-4-5`) via config. OpenAI remains the default provider to preserve current behavior.
