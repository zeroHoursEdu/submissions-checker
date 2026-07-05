## Why

`provision_test_student` creates a `StudentAssignment` row per assignment with `variant` left
`NULL` (`docs/known_bugs.md` #12b). For any assignment with `variants_required: true` — the
pythonBasics subject sets this on every one of its labs — `check_core.resolve_check_plan` rejects
the submission with "Your variant has not been assigned yet," making the test-student QA feature
dead on arrival for exactly the subjects that most need dry-run verification. The only place
variants get set today is the CSV enrollment-import flow, which the test-student feature bypasses
entirely. There is no UI at all today to pick a variant for the test student — just a single
"Create Test Student" button.

## What Changes

- The "Create Test Student" form on the subject page gains one variant dropdown per assignment
  that declares a non-empty `variants` block in its config, populated from that assignment's
  actual variant keys (the same config source the CSV template's `variant_<code>` columns read).
- `provision_test_student` reads the submitted `variant_<code>` selections and sets
  `StudentAssignment.variant` accordingly. For any assignment with `variants_required: true`
  that the teacher didn't explicitly pick (or submitted an invalid key for), the first variant
  key (sorted) is assigned automatically — so the test student is always immediately able to
  submit to every assignment, closing bug #12b outright rather than just exposing a picker.

## Capabilities

### New Capabilities
- `test-student-provisioning`: defines the test-student QA feature's behavior, including variant
  assignment, since it was previously undocumented by any spec.

## Impact

- `templates/teacher_subject.html` — variant `<select>` per variants-bearing assignment, added
  to the existing "Create Test Student" form.
- `src/submissions_checker/api/routes/teacher_portal.py` — `provision_test_student` reads
  `variant_<code>` form fields and sets `StudentAssignment.variant`.
- No schema change: `StudentAssignment.variant` already exists and is nullable.
