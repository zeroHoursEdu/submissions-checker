# ai-review

## Purpose

Defines the AI review gate that runs after a submission passes its automated tests and
before the student is granted the quiz: a provider-agnostic (Claude/OpenAI, one active at a
time) review that produces a structured verdict (cheating, AI-generated, code-quality mark,
comment), gates the quiz or escalates flagged work to a teacher, and optionally surfaces the
AI comment to the student.

## Requirements

### Requirement: AI review runs after tests and before the quiz

The system SHALL support a per-assignment review mode in which, after a submission passes its automated tests, an AI review runs before the student is granted access to the quiz. This mode is selected by `assignments.<code>.review_mode: tests_then_ai_then_quiz` in the subject config. When AI review is not configured for the assignment, the submission flow SHALL behave exactly as it does today (no AI call).

#### Scenario: Tests pass triggers AI review before quiz

- **WHEN** a submission whose assignment config has `review_mode: tests_then_ai_then_quiz` passes its automated tests
- **THEN** the submission transitions to `AWAITING_AI_REVIEW`, a `RUN_AI_REVIEW` outbox message is enqueued in the same transaction, and the student is NOT yet able to start the quiz

#### Scenario: AI review skipped when not configured

- **WHEN** a submission whose assignment config uses `review_mode: tests_then_quiz` (or `tests_only`) passes its tests
- **THEN** no AI call is made and the submission follows the existing quiz/completed flow unchanged

#### Scenario: AI review does not run before tests pass

- **WHEN** a submission fails its automated tests under `review_mode: tests_then_ai_then_quiz`
- **THEN** the submission transitions to `TEST_FAILED` and no AI review is performed

### Requirement: AI review produces a structured verdict

The AI review SHALL request a structured result from the configured provider and persist it to `submissions.ai_review`. The result SHALL contain: a cheating verdict (`is_cheating` boolean and a `confidence` in [0,1]), an AI-generated verdict (`is_ai_generated` boolean and a `confidence` in [0,1]), a `code_mark` integer in [0,100] representing code quality, a free-text `comment`, and the `provider` and `model` used. When the provider returns malformed output that cannot be parsed into this structure, the review SHALL be treated as failed.

#### Scenario: Structured verdict persisted

- **WHEN** the AI review completes successfully
- **THEN** `submissions.ai_review` holds the cheating verdict, AI-generated verdict, `code_mark`, `comment`, `provider`, and `model`

#### Scenario: Malformed provider output fails the review

- **WHEN** the provider returns output that cannot be parsed into the required structure
- **THEN** the submission transitions to `AI_REVIEW_FAILED` and the outbox message is retried according to the existing retry policy

### Requirement: AI verdict gates the quiz or escalates to a teacher

When the AI review passes — the work is judged neither cheating nor AI-generated above the configured thresholds — the submission SHALL be advanced to `QUIZ_SENT` so the student may take the quiz. When the AI review flags the work as cheating or AI-generated at or above the configured confidence thresholds, the submission SHALL be routed to `AWAITING_TEACHER_REVIEW` for a human decision rather than auto-failing, and a teacher-review notification SHALL be enqueued. The `code_mark` SHALL be recorded regardless of the pass/flag outcome.

Thresholds are read from `assignments.<code>.ai_review`: `cheating_threshold` and `ai_generated_threshold` (each a confidence in [0,1], default 0.5).

#### Scenario: Clean work advances to the quiz

- **WHEN** the AI verdict reports `is_cheating: false` and `is_ai_generated: false` (or confidences below the configured thresholds)
- **THEN** the submission transitions to `QUIZ_SENT` and the student can start the quiz

#### Scenario: Flagged work is escalated to a teacher

- **WHEN** the AI verdict reports cheating or AI-generated authorship at or above the configured threshold
- **THEN** the submission transitions to `AWAITING_TEACHER_REVIEW`, a teacher-review notification is enqueued, and the student is NOT granted the quiz

#### Scenario: Code mark is stored on flag

- **WHEN** the AI review flags the work and escalates to a teacher
- **THEN** `submissions.ai_review.code_mark` is still persisted for later grade calculation

### Requirement: Provider is selectable and single-active

The system SHALL support two AI providers — Anthropic (Claude) and OpenAI — with exactly one active at a time, selected by the application setting `ai_provider` (`anthropic` or `openai`). The model and API key for the active provider SHALL come from application settings (`anthropic_model`/`anthropic_api_key` or `openai_model`/`openai_api_key`). The AI review SHALL call only the active provider; the inactive provider's credentials are not required.

#### Scenario: OpenAI provider selected

- **WHEN** `ai_provider = openai` and an AI review runs
- **THEN** the review calls the OpenAI API using `openai_model` and `openai_api_key`, and records `provider: "openai"` and the model in the result

#### Scenario: Anthropic provider selected

- **WHEN** `ai_provider = anthropic` and an AI review runs
- **THEN** the review calls the Anthropic API using `anthropic_model` and `anthropic_api_key`, and records `provider: "anthropic"` and the model in the result

#### Scenario: Invalid provider rejected at startup

- **WHEN** `ai_provider` is set to a value other than `anthropic` or `openai`
- **THEN** application settings validation SHALL reject the value

### Requirement: Student can see the AI comment when the subject enables it

When `assignments.<code>.ai_review.show_comment_to_student` is true, the AI review's `comment` SHALL be shown to the student on the assignment-detail page for that submission. When the toggle is false or absent, the comment SHALL NOT be shown to the student. The cheating/AI-generated verdicts and confidences are never shown to the student regardless of the toggle.

#### Scenario: Comment shown when enabled

- **WHEN** an assignment has `ai_review.show_comment_to_student: true` and the submission has an AI comment
- **THEN** the student's assignment-detail page displays the AI comment

#### Scenario: Comment hidden when disabled

- **WHEN** an assignment has `ai_review.show_comment_to_student: false` (or omitted)
- **THEN** the student's assignment-detail page does not display any AI comment, even though it is stored on the submission

#### Scenario: Verdicts never shown to student

- **WHEN** the AI comment is shown to the student
- **THEN** the raw cheating/AI-generated verdicts and confidence scores are not included in what the student sees
