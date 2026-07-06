# grade-calculation

## Purpose

Defines how a submission's final grade is computed as a configurable weighted blend of a code
score (itself a blend of test-pass performance and the AI code-quality mark) and a quiz score,
persisted to `StudentAssignment.grade` at every completion point, with a stored breakdown and
a student-visibility toggle.

## Requirements

### Requirement: Final grade is a configurable weighted blend

The system SHALL compute a submission's final grade as a configurable weighted blend of a **code score** and a **quiz score**, and the code score SHALL itself be a configurable weighted blend of a **works score** (the fraction of test points earned) and a **quality score** (the AI code mark). The weights come from `assignments.<code>.grading`:

- `code_weight` and `quiz_weight` — the split between code and quiz (default `code_weight: 1.0`, `quiz_weight: 0.0`).
- `code.works_weight` and `code.quality_weight` — the split, within the code score, between test-pass performance and AI code-quality (default `works_weight: 1.0`, `quality_weight: 0.0`).

All component scores are normalized to a 0–100 scale before weighting. The final grade SHALL be scaled to the assignment's `[min_grade, max_grade]` range and clamped to it. When a weighted component has no data available (e.g. no quiz was taken, or no AI mark exists), its weight SHALL be treated as zero and the remaining weights renormalized so the grade still reflects the components that do exist.

#### Scenario: Blend of works, quality, and quiz

- **WHEN** an assignment configures `code_weight: 0.6, quiz_weight: 0.4` and `code.works_weight: 0.7, code.quality_weight: 0.3`, and a submission earned 90% of test points, an AI code mark of 80, and a quiz score of 50%
- **THEN** the code score is `0.7×90 + 0.3×80 = 87`, the blended 0–100 grade is `0.6×87 + 0.4×50 = 72.2`, scaled into the assignment's `[min_grade, max_grade]` range

#### Scenario: Missing quiz renormalizes weights

- **WHEN** an assignment configures a non-zero `quiz_weight` but the submission path never produced a quiz score (e.g. AI review completed the submission without a quiz)
- **THEN** the quiz component is dropped and the grade is computed from the code score alone (its weight renormalized to 1.0)

#### Scenario: Missing AI mark renormalizes code weights

- **WHEN** an assignment has a non-zero `code.quality_weight` but no AI review ran (no `code_mark` exists)
- **THEN** the quality component is dropped and the code score is computed from the works score alone

#### Scenario: Grade clamped to assignment range

- **WHEN** the computed blended grade scales outside `[min_grade, max_grade]`
- **THEN** the persisted grade is clamped into that range

### Requirement: Grade is persisted when a submission completes

The system SHALL compute and persist the final grade to `StudentAssignment.grade` at the moment a submission reaches a terminal completed state, on every completion path — the quiz-finalization path, the AI-review-to-completed path, and the tests-only completion path. The computed breakdown (component scores and the weights applied) SHALL be persisted to `submissions.grade_breakdown`.

#### Scenario: Grade written on quiz completion

- **WHEN** a student's quiz attempt is finalized and the submission transitions to `COMPLETED`
- **THEN** `StudentAssignment.grade` is set from the weighted blend and `submissions.grade_breakdown` records the component scores and weights

#### Scenario: Grade written on non-quiz completion

- **WHEN** a submission completes without a quiz (e.g. `tests_only`, or AI review that completes the submission)
- **THEN** `StudentAssignment.grade` is still computed and persisted from the available components

#### Scenario: Breakdown stored alongside grade

- **WHEN** a grade is persisted
- **THEN** `submissions.grade_breakdown` contains the works score, quality score (if any), quiz score (if any), the weights applied, and the final grade

### Requirement: Student can see the grade breakdown when the subject enables it

When `assignments.<code>.grading.show_breakdown_to_student` is true, the student's assignment-detail page SHALL display how the grade was calculated — the works score, the AI quality score (if used), the quiz score (if used), and the weights applied. When the toggle is false or absent, only the final grade SHALL be shown, not the breakdown.

#### Scenario: Breakdown shown when enabled

- **WHEN** an assignment has `grading.show_breakdown_to_student: true` and the submission has a stored breakdown
- **THEN** the student's assignment-detail page displays the component scores and weights that produced the grade

#### Scenario: Breakdown hidden when disabled

- **WHEN** an assignment has `grading.show_breakdown_to_student: false` (or omitted)
- **THEN** the student sees only the final grade, and the breakdown is not displayed
