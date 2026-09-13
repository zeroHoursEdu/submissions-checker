## Why

Some subjects cannot be auto-checked at all. The incoming WinAPI/C++ subject
(«Основи розробки розподіленого програмного забезпечення») is seven Windows-only GUI labs —
there is nothing a Linux sandbox can run. Its students must attach their report plus sources
and then be examined by a quiz that stands in for the oral defence («захист»). Today that is
impossible: every `review_mode` begins with the sandbox, and `resolve_check_plan` hard-fails an
assignment that declares no `check_command`, so such a submission lands in `VALIDATION_FAILED`
instead of reaching a quiz.

The same subject needs the quiz to be a real exam gate. The platform can time a whole attempt
(`time_limit_minutes`) but cannot time an individual question, so an easy recall item and a
tricky multi-select item get the same budget and a student can spend the entire attempt looking
up one answer.

## What Changes

- Add two review modes that accept a submission without running any checks:
  - `quiz_only` — safe-archive validation only, then straight to the quiz; grade from the quiz.
  - `quiz_then_teacher` — the same, but a passed quiz routes the submission to the teacher for
    review of the attached work rather than completing it.
  No Docker container is started for either, and neither requires a `sandbox` block in the
  subject config.
- Add per-question time limits to quizzes: `time_limit_seconds` on a question and
  `question_time_default_seconds` at quiz level.
- When any drawn question carries a limit, the quiz is delivered as a **server-authoritative
  stepper**: one question per page, its own countdown, no going back. A question whose window
  elapses is recorded as answered-late with zero points and flagged `timed_out`, whether or not
  the browser was open. Timed-out questions are highlighted on the result page.
- Anti-cheat `reduce_time` penalties apply to the current question's remaining time in stepper
  mode (they continue to apply to the whole attempt in the existing one-page mode).
- Document the quiz config block in the plugin-authoring guide, which currently documents none
  of it.
- Not breaking: a quiz with no per-question limits keeps the existing single-page form, and all
  existing review modes are untouched.

## Capabilities

### New Capabilities
- `quiz-timing`: per-question time limits, stepper delivery of a timed quiz, server-side expiry
  of a question, and the recording and surfacing of timed-out answers.

### Modified Capabilities
- `assignment-checking`: new `quiz_only` / `quiz_then_teacher` review modes that bypass the
  sandbox entirely; an assignment in these modes is valid with no `check_command`; the
  post-quiz transition to teacher review.
- `quiz-proctoring`: `reduce_time` is defined against the current question's remaining time when
  the attempt is in stepper mode.

## Impact

- `workers/tasks/check_tasks.py` — quiz-first branch ahead of check-plan resolution.
- `core/state_machine.py` — `QUIZ_SENT` gains explicit transitions (today the quiz route mutates
  `submission.status` directly).
- `api/routes/student_quiz.py` — attempt snapshot gains `review_mode` and per-question seconds;
  new stepper GET/POST flow alongside the existing one-page flow.
- `api/routes/teacher_portal.py` — the approve handler must not bounce a submission that already
  passed its quiz back into the quiz.
- `db/models/quiz_template.py` + a new Alembic revision — attempt cursor/clock columns and a
  `timed_out` flag on answers.
- `templates/` — a new stepper page, extraction of the shared anti-cheat script into an include,
  timed-out badges on the result page; new `i18n/uk.yml` keys.
- `docs/PLUGIN_AUTHORING.md`, `docs/anti-cheat.md`.
- No change to `services/grading.py`: an assignment with no test results already renormalises
  onto the quiz weight.
