## ADDED Requirements

### Requirement: Teacher selects a variant per assignment when provisioning a test student
When provisioning a test student, for each assignment whose config declares a non-empty
`variants` block, the teacher SHALL be presented with a selector populated from that assignment's
actual variant keys. The selected value SHALL be persisted on the created `StudentAssignment` row.

#### Scenario: Variant options come from config
- **WHEN** a teacher opens the "Create Test Student" form for a subject whose assignment `lab1`
  declares `variants: {"1": ..., "2": ...}`
- **THEN** the form shows a selector for `lab1` offering exactly `1` and `2` as choices

#### Scenario: Selected variant is persisted
- **WHEN** a teacher picks variant `2` for `lab1` and submits the form
- **THEN** the test student's `StudentAssignment` row for `lab1` has `variant = "2"`

### Requirement: Test student can always submit to variants_required assignments
For any assignment with `variants_required: true`, the provisioned test student SHALL have a
valid `variant` assigned regardless of whether the teacher explicitly picked one, so the test
student can submit immediately without hitting a "variant not assigned" error.

#### Scenario: No selection made — first variant assigned automatically
- **WHEN** a teacher creates a test student without changing any variant selector, for an
  assignment with `variants_required: true` and variants `{"1": ..., "2": ...}`
- **THEN** the test student's `StudentAssignment.variant` for that assignment is set to `"1"`
  (the first variant key, sorted), not left `NULL`

#### Scenario: Invalid submitted variant falls back to the default
- **WHEN** a submitted variant selection does not match any key in that assignment's configured
  `variants`
- **THEN** the first variant key (sorted) is assigned instead of the invalid value

#### Scenario: Misconfigured assignment with no variants defined
- **WHEN** an assignment has `variants_required: true` but no `variants` block (or an empty one)
  in its config
- **THEN** the test student's `variant` for that assignment remains `NULL`, unchanged from
  today's behavior — this is a subject-config problem, not something provisioning papers over
