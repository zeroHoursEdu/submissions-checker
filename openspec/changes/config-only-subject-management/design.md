## Context

The teacher portal grew two contradictory ways of changing a subject. The intended one is Apply
config: a ZIP whose `config.yml` is versioned in a subject repo, reviewed in a diff, stored as a
numbered `SubjectPluginConfig` row, and frozen into every submission and quiz attempt it produces.
The accidental one is a scatter of CRUD forms — subject edit, subject delete, assignment create,
assignment edit, and a quiz editor — that write straight to `subjects` and
`subjects_assignments`. Anything entered there is invisible to the repo and is overwritten by the
next re-apply without warning. The quiz editor is worse than useless: `teacher_quiz_editor.html`
posts to `/quiz/import`, `/quiz/config`, `/quiz/questions` and `/quiz/export`, none of which
exist (`docs/known_bugs.md:166`).

Two more dead affordances sit alongside them. The dashboard links to `/teacher/analytics`, which
is declared `AdminUser` (`analytics.py:36`) and answers 403 to every ordinary teacher. Export CSV
appears on both the subject and assignment pages.

The genuinely missing capability is enrolment. `teacher_subject.html:91-112` lists enrolled
students read-only. `POST /teacher/subjects/{id}/students/import` and
`POST /teacher/subjects/{id}/enroll/{sid}` both exist and both work, but no template links to
either, and both are POST-only, so a teacher cannot reach them by typing a URL. The only way to
enrol anyone today is hand-written SQL against `subjects_students` and `students_assignments`.

## Goals / Non-Goals

**Goals:**

- Make config re-apply the only route by which subject and assignment content changes, so what
  runs always matches what is in the subject repo.
- Give teachers a working enrolment path: upload `email,variant`, get students into the subject
  with their variants set.
- Tell the teacher exactly which rows failed and why, rather than silently skipping them as the
  current importers do.
- Generate the example CSV from the subject's real config, so the variant identifiers a teacher
  copies are the ones the checker will actually accept.

**Non-Goals:**

- Deleting the routes behind the removed buttons. They keep their own authorization; only the UI
  entry points go. Removing endpoints is a separate, larger decision.
- Creating students, groups or accounts from the enrolment CSV, or sending invitations. That
  stays with `POST /teacher/students/import`.
- Per-assignment variants in this file. One variant per student applies to the whole subject.
- Any schema change. Every column this needs already exists.
- Fixing `teacher_quiz_editor.html` itself. Unlinking it is in scope; deleting the template and
  closing out `docs/known_bugs.md:166` is not.

## Decisions

### Remove the buttons, keep the routes

Deleting the endpoints would break any admin script or test that calls them and would force
`subject-management`'s delete requirement to be withdrawn outright rather than narrowed. Removing
only the template affordances achieves the actual goal — no teacher can casually diverge from the
config — at a fraction of the blast radius. The spec states this explicitly so a future reader
does not "helpfully" re-add a button.

Alternative considered: gate the buttons behind a feature flag. Rejected — a flag implies the UI
path is a supported mode, which is exactly the ambiguity this change is removing.

### One `variant` column, applied subject-wide

`students_assignments.variant` is `String(50)` and nullable, one row per (student, assignment), so
the data model can express a different variant per assignment. The existing per-subject template
exposes that as one `variant_<code>` column per assignment.

That shape is rejected here. A file whose columns change whenever the config changes is hard to
hand-write and hard to explain, and in practice a student carries one variant number for the whole
course. The new file is `email,variant`; the value is written to every `students_assignments` row
of the subject. Teachers who genuinely need per-assignment variants can still set them through the
existing per-assignment path.

Consequence to state plainly: this narrows what the import endpoint can express. The endpoint is
being repurposed, not extended, and the old `student_group,student_name,student_surname,email,
variant_*` format will no longer be accepted by it.

### Enroll-only, e-mail as the key

Creating students needs `student_group`, `student_name` and `student_surname`, because
`students.group_id` is `NOT NULL` and `full_name` is required — which drags the invite e-mail,
username generation and password generation along with it. Keeping creation in the global import
leaves this endpoint with one job and makes its failure mode obvious: an address either belongs to
a registered student or it does not.

The two-step flow becomes: global import creates accounts and sends credentials, then per-subject
CSV enrols them. This also means enrolment can be re-run freely without re-inviting anyone.

### Report rejections through the redirect URL, capped

`apply-config` already flashes messages by round-tripping them through query parameters and
`urllib.parse.unquote` (`teacher_portal.py:106-110`). Enrolment follows the same pattern rather
than introducing session-backed flash storage for one feature.

Counts go as plain integers. Rejected rows go as a single parameter holding `line:reason` pairs,
URL-encoded, **capped at 20 entries** with an "and N more" count, so a 500-row CSV of bad
addresses cannot produce a URL that the browser or proxy truncates. Reasons are a small closed
set (`unknown`, `empty`) mapped to vocabulary strings at render time, not free text, which keeps
the URL short and the message translatable.

Alternative considered: render the result page directly instead of redirecting. Rejected — it
breaks the POST-redirect-GET pattern every other form on the page follows, and a refresh would
re-submit the file.

### Example CSV reads the active config, merged across assignments

**Revised during implementation.** The design originally called for loading the subject's
highest-version `SubjectPluginConfig`. That turned out to be an unnecessary second query:
`config_apply._build_assignment_config` copies `variants` into each
`subjects_assignments.config` on every apply, so the assignment rows already carry the same
ids as the plugin config. The endpoint reads them from there instead — the same source the
test-student panel already uses (`teacher_subject.html:176`). Behaviour is identical, since
both are written from the same config at the same moment.

The endpoint collects the union of `variants` keys across the subject's assignments. Union
rather than per-assignment because the file has one variant column; a variant offered by any
assignment is a legitimate value to type.

Keys are emitted verbatim and in sorted order. Sorting is lexicographic on the string form, since
variant identifiers are strings in the config and `String(50)` in the database; numeric-looking
ids are sorted numerically when every key parses as an integer, so `2` does not follow `10`.

Placeholder addresses use the `example.invalid` domain, which RFC 6761 reserves and guarantees
will never resolve or belong to a real person, so an accidentally uploaded unmodified template
enrols nobody and the rejection report says so clearly.

### Where the enrolment panel lives

It goes on the subject page, in the slot the removed edit/export/delete buttons vacate, next to
the enrolled-students list it affects. `teacher_subject` already loads the subject, its students
and its assignments; it gains only the flash values parsed from query parameters.

## Risks / Trade-offs

- **The import endpoint's contract changes shape.** Anyone with a saved
  `student_group,...,variant_lab1` file for the per-subject importer will get a 422 naming `email`
  as missing. → The old format still works at the global `POST /teacher/students/import`, which is
  untouched; the subject page links the new template so the current shape is always one click
  away.

- **A teacher loses the ability to fix a typo without a re-upload.** Correcting one assignment
  title now means editing `config.yml`, re-zipping and re-applying. → That is the point, and the
  cost is small: re-apply is idempotent, content-hash deduplicated, and in-flight quiz attempts
  finish on the version they started with.

- **Admins lose the dashboard link to a page they can use.** → The page stays reachable at
  `/teacher/analytics`; only the link goes. Restoring it behind a role check is a one-line change
  if it turns out to be missed.

- **Variant written subject-wide overwrites per-assignment variants set earlier.** A re-upload
  with a non-empty variant flattens any per-assignment differences. → An empty `variant` cell is
  explicitly defined to leave existing values untouched, so a teacher who has per-assignment
  variants can enrol without disturbing them.

- **Unlinking the quiz editor leaves a dead template in the tree.** → Recorded already at
  `docs/known_bugs.md:166`; this change updates that entry to note the link is gone, leaving only
  the template to delete later.

## Migration Plan

No data migration. The change is template edits plus two reworked handlers, so deployment is the
ordinary image roll and rollback is redeploying the previous tag. The only external visibility is
that teachers see fewer buttons and one new panel.

Docs to correct in the same change: `docs/teacher_journey_guide.md` and `docs/feature_catalog.md`
both describe affordances that will no longer exist.

## Open Questions

- Should the unused vocabulary keys behind the removed buttons (`export_csv`, `remove_subject_*`,
  `quiz_export_json`) be deleted from `i18n/uk.yml`, or left for whenever the routes themselves
  are removed? Leaving them costs nothing and keeps the eventual route removal a one-file change;
  this change leaves them in place.
