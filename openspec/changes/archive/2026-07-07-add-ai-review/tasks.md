## 1. Config & dependencies

- [x] 1.1 Add `anthropic>=0.40` to `pyproject.toml` dependencies (alongside `openai`).
- [x] 1.2 In `core/config.py`: change `ai_provider` to `Literal["openai", "anthropic"] = "openai"`; add `anthropic_api_key: str | None = None` and `anthropic_model: str = "claude-opus-4-8"`. Keep existing `openai_*` and `ai_max_tokens`.

## 2. Provider abstraction

- [x] 2.1 Create `services/ai/provider.py` with an `AIProvider` protocol (`async def review(system, user, schema) -> dict`), an `OpenAIProvider` (AsyncOpenAI, `response_format={"type":"json_object"}`), and an `AnthropicProvider` (AsyncAnthropic, `output_config.format` json_schema, no temperature).
- [x] 2.2 Add `get_ai_provider(settings)` factory selecting on `settings.ai_provider`, raising a clear error if the active provider's API key is unset.
- [x] 2.3 Delete the dead `services/ai/client.py` and `services/ai/code_reviewer.py` skeletons (confirm no imports).

## 3. AI review handler & state machine

- [x] 3.1 In `core/state_machine.py`: add `AI_REVIEWING: {"ai_review_passed_quiz": SubmissionStatus.QUIZ_SENT}` to `_TRANSITIONS`.
- [x] 3.2 In `check_tasks.py::_advance_after_tests`: add `review_mode == "tests_then_ai_then_quiz"` branch — `transition(test_passed_ai)` + enqueue `RUN_AI_REVIEW` with `payload={"submission_id":..., "next_step":"quiz"}`.
- [x] 3.3 Rewrite `workers/tasks/review_tasks.py::execute_ai_review_task` to call `get_ai_provider(...)` with a verdict schema, persist the structured result to `submission.ai_review`, and treat parse failure as `ai_review_failed` (raise for retry).
- [x] 3.4 In the handler, for `next_step == "quiz"`: read `cheating_threshold`/`ai_generated_threshold` (default 0.5) from the assignment's `ai_review` config; flagged → `ai_review_done_teacher` + `enqueue_teacher_review_notification`; clean → `ai_review_passed_quiz`. Preserve existing `teacher`/`completed` behavior.

## 4. Grade calculation

- [x] 4.1 Alembic migration: add nullable `grade_breakdown JSONB` to `submissions`; add the mapped column to `db/models/submission.py`.
- [x] 4.2 Create `services/grading.py`: pure `compute_grade(grading_cfg, min_grade, max_grade, *, works_pct, ai_mark, quiz_pct)` handling absent components (drop + renormalize) and clamping to `[min_grade, max_grade]`; return grade + breakdown.
- [x] 4.3 Add `finalize_grade(db, submission)` that reads works% from `test_results`, ai_mark from `ai_review`, quiz% from the passed `QuizAttempt`, then writes `StudentAssignment.grade` and `submissions.grade_breakdown`.
- [x] 4.4 Call `finalize_grade` at every completion point: `student_quiz.py::_grade_and_finalize` (on COMPLETED), the tests-only branch of `_advance_after_tests`, the `ai_review_done_completed` path in `review_tasks.py`, and the teacher-approve→COMPLETED path in `teacher_portal.py`.

## 5. Config apply

- [x] 5.1 In `services/config_apply.py`: add `"ai_review"` and `"grading"` to the assignment allow-list in `_build_assignment_config` and to the diff `config_keys` list.

## 6. Student portal display

- [x] 6.1 Add `ai_comment: str | None` and `grade_breakdown: dict | None` to the `AssignmentDetail` schema.
- [x] 6.2 In `student_portal.py::assignment_detail`: populate `ai_comment` only when the assignment's `ai_review.show_comment_to_student` is true, and `grade_breakdown` only when `grading.show_breakdown_to_student` is true. Never expose verdicts/confidences.
- [x] 6.3 In `templates/assignment_detail.html`: render the AI comment block and the grade-breakdown block behind those fields.

## 7. Tests & verification

- [x] 7.1 Unit-test `compute_grade`: full blend, missing quiz, missing AI mark, clamping.
- [x] 7.2 Test the provider factory (selection + missing-key error) with both providers mocked.
- [x] 7.3 Functional test the `tests_then_ai_then_quiz` flow: clean verdict → `QUIZ_SENT`; flagged verdict → `AWAITING_TEACHER_REVIEW`; parse failure → `AI_REVIEW_FAILED` (provider mocked).
- [x] 7.4 Test config-apply persists/diffs `ai_review` and `grading`, and the two student-visibility toggles (comment shown/hidden, breakdown shown/hidden).
- [x] 7.5 Run `make lint`, `make type-check`, and the test suite; verify the AI-gate flow end-to-end with a mocked provider.
