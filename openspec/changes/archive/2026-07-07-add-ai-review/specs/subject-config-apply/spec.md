## ADDED Requirements

### Requirement: Config apply persists AI-review and grading blocks

The subject config-apply service SHALL include the per-assignment `ai_review` and `grading` blocks in the set of assignment config fields it persists to `subjects_assignments.config` and compares during its field-level diff. A change to either block SHALL be detected as a change to the assignment's config so the assignment row is updated, and an unchanged block SHALL NOT trigger a spurious update.

#### Scenario: AI-review block persisted on apply

- **WHEN** a teacher uploads a ZIP whose assignment defines an `ai_review` block
- **THEN** the `ai_review` block is stored under `subjects_assignments.config` for that assignment

#### Scenario: Grading block persisted on apply

- **WHEN** a teacher uploads a ZIP whose assignment defines a `grading` block
- **THEN** the `grading` block is stored under `subjects_assignments.config` for that assignment

#### Scenario: Change to grading weights is diffed

- **WHEN** a subsequent upload changes only an assignment's `grading.code_weight`
- **THEN** the field-level diff detects the assignment config changed and updates that assignment, without reporting unrelated assignments as changed

#### Scenario: Unchanged blocks cause no update

- **WHEN** a re-upload leaves `ai_review` and `grading` byte-identical
- **THEN** neither block causes the assignment to be reported as changed
