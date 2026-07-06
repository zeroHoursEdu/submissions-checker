## Why

Today a submission that passes automated tests goes straight to the quiz (or teacher review) with no defense against plagiarism or AI-generated code, and `StudentAssignment.grade` is never actually computed — the platform records test results and quiz scores but never combines them into a grade. Teachers want an AI gate that vets a submission for cheating and AI authorship before letting the student proceed, assigns a code-quality mark, and a grade that blends "does it work" (tests), "is the code good" (AI mark), and quiz performance by configurable weights.

The existing AI-review scaffolding (`review_tasks.py`, the `AWAITING_AI_REVIEW`/`AI_REVIEWING` states, the `RUN_AI_REVIEW` outbox event) is OpenAI-only, produces a freeform blob, and only ever routes to teacher-review or completion. This change turns that scaffolding into a real, provider-agnostic AI review that gates the quiz and drives grading.

## What Changes

- **AI review gate before the quiz.** New per-assignment `review_mode: tests_then_ai_then_quiz`. After tests pass, the AI review runs; if the work passes the AI's cheating/AI-generated checks the student is allowed into the quiz (`QUIZ_SENT`), otherwise the submission is routed to teacher review for a human decision.
- **Structured AI verdict.** The AI returns a structured result: cheating verdict, AI-generated verdict (each with confidence), a 0–100 **code-quality mark**, and a free-text **comment**. Stored on `submissions.ai_review`.
- **Provider abstraction — Claude + OpenAI.** A single active provider chosen by app config (`ai_provider = openai | anthropic`), with per-provider model and API key. Adds the `anthropic` SDK dependency and Anthropic settings. Exactly one provider is active at a time.
- **Configurable weighted grade.** New per-assignment `grading` config: the final grade is `code_weight` × code-score + `quiz_weight` × quiz-score, where code-score itself blends `works_weight` (test pass %) and `quality_weight` (AI code mark). A new grading service computes and persists `StudentAssignment.grade` at the moment a submission completes (both the quiz and non-quiz paths), plus a stored breakdown.
- **Student-visible AI comment (toggle).** `ai_review.show_comment_to_student` controls whether the AI comment is surfaced on the student's assignment-detail page.
- **Student-visible grade breakdown (toggle).** `grading.show_breakdown_to_student` controls whether the grade breakdown (code vs quiz, works vs quality) is shown to the student.
- Extends the config-apply allow-list so the new `ai_review` and `grading` blocks persist and diff correctly.

## Capabilities

### New Capabilities
- `ai-review`: AI review gate that runs after tests and before the quiz — provider-agnostic (Claude/OpenAI, one active), structured cheating + AI-generated verdict, code-quality mark, student-visible comment toggle, and quiz-gating vs teacher-escalation behavior.
- `grade-calculation`: Configurable weighted final grade combining test pass rate, AI code mark, and quiz score; persisted to `StudentAssignment.grade` with a stored breakdown and a student-visibility toggle.

### Modified Capabilities
- `subject-config-apply`: The per-assignment config allow-list and field-level diff MUST include the new `ai_review` and `grading` blocks so they persist to `subjects_assignments.config` and are diffed on re-upload.

## Impact

- **Config (app):** `core/config.py` — add `anthropic_api_key`, `anthropic_model`; `ai_provider` becomes a validated `openai | anthropic` literal; keep existing `openai_*`.
- **Dependencies:** add `anthropic>=0.40` to `pyproject.toml` (alongside existing `openai`).
- **AI services:** replace the dead `services/ai/` skeletons with a real provider abstraction (`services/ai/provider.py`) and rewrite `workers/tasks/review_tasks.py` to produce the structured verdict and gate the quiz.
- **State machine:** `core/state_machine.py` — add `AI_REVIEWING → QUIZ_SENT` transition; new review-mode branch in `workers/tasks/check_tasks.py::_advance_after_tests`.
- **Grading:** new `services/grading.py`; call sites at both completion points (`api/routes/student_quiz.py::_grade_and_finalize` and the non-quiz `_advance_after_tests`/AI-review completion path).
- **DB:** Alembic migration adding `submissions.grade_breakdown JSONB` (nullable). No enum changes — the needed statuses/events already exist.
- **Config apply:** `services/config_apply.py` allow-list + diff key lists.
- **Student portal:** `api/routes/student_portal.py` + `api/schemas/student_portal.py` + `templates/assignment_detail.html` — surface AI comment and grade breakdown under their toggles.
- **Tests:** functional coverage for the provider abstraction, the AI-gate branch, grade calculation, and the two config toggles.
