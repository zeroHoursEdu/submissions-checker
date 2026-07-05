## Context

`SubjectsAssignment.config` is a JSONB blob that may contain a `variants` dict (`{"1": {...},
"2": {...}}`, keys are variant IDs, values are command overrides — see
`docs/PLUGIN_AUTHORING.md`). The CSV enrollment-import flow is the only existing place that sets
`StudentAssignment.variant`, driven by `variant_<code>` columns generated when
`variants_required` is true. `provision_test_student` never touches `variant` at all.

## Goals / Non-Goals

**Goals:** let the teacher pick a variant per assignment when creating the test student, sourced
from real config; guarantee the test student can submit successfully out of the box even if the
teacher doesn't touch the picker.

**Non-Goals:** changing the CSV import flow or the general variant-assignment model — this only
adds a second entry point (test-student provisioning) that writes to the same
`StudentAssignment.variant` column the CSV flow already writes to.

## Decisions

**Default to the first variant key (sorted) when unpicked, for `variants_required` assignments.**
The literal ask is "let teacher choose" — but a picker alone doesn't fix bug #12b if the teacher
just clicks the button without touching the dropdowns (the default HTML `<select>` behavior
already selects the first `<option>`, so in practice this happens automatically as long as the
first option isn't a blank "none" placeholder). Deciding not to include a blank/"unassigned"
option for `variants_required` assignments' dropdowns is what actually closes the bug — for
assignments where `variants_required` is false but `variants` is still configured, a blank
"No variant" option is included, since submission there isn't gated on having one.

**Read variant selections via `await request.form()`, not typed `Form(...)` params.** The set of
`variant_<code>` fields is dynamic (one per assignment, unknown at route-signature time) — this
matches how the CSV import path already parses dynamic `variant_<code>` columns from arbitrary
data rather than fixed parameters.

**Validate the submitted key against the assignment's actual `variants` dict.** A submitted
`variant_<code>` value that isn't a real key for that assignment (stale form, tampered request)
falls back to the same first-key default rather than being written verbatim — avoids ever setting
a variant value that doesn't correspond to a real check-command override.

## Risks / Trade-offs

- [Risk] An assignment's `variants` dict could be empty/missing entirely while
  `variants_required` is true (misconfigured subject) — no key exists to default to.
  → Mitigation: leave `variant` `NULL` in that case, identical to today's behavior; this is a
  pre-existing subject-config problem, not something test-student provisioning should paper over.
