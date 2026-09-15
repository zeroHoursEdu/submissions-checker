## Why

A subject's content already has one source of truth — the config ZIP a teacher uploads through
Apply config, which is version-controlled in the subject repo, reviewable in a diff, and stored
as a numbered `SubjectPluginConfig` row. Yet the teacher UI still offers buttons to edit a
subject, create and edit assignments, and open a quiz editor. Any change made that way exists
only in the database: the next config re-apply silently overwrites it, and nothing in the repo
records that it ever happened. The dashboard also links to Analytics, which requires
`AdminUser` (`analytics.py:36`) and returns 403 for every ordinary teacher.

Meanwhile the one thing a teacher genuinely must do outside the config — put students into a
subject and give each a variant — has no UI at all. The subject page lists enrolled students
read-only; `POST /teacher/subjects/{id}/students/import` and
`POST /teacher/subjects/{id}/enroll/{sid}` exist but nothing links to them, and both are
POST-only, so they cannot be reached by typing a URL either. Enrolling anyone currently requires
hand-written SQL.

## What Changes

- **BREAKING (UI)**: Every affordance that mutates subject or assignment content is removed from
  the teacher UI. Subject edit, subject delete, create assignment, edit assignment, the quiz
  editor link, and both Export CSV buttons. The underlying routes are left in place; only the
  entry points go, so nothing that currently works via API breaks.
- The Analytics button is removed from the teacher dashboard. The page is admin-only and the
  button was dead for teachers.
- A new **Enroll students** panel on the subject page accepts a two-column CSV, `email,variant`.
  Each row enrolls an existing student in that subject and writes the variant to every
  `students_assignments` row the enrolment creates.
- Enrolment is **enroll-only**: `email` must match an existing `students` row. Unknown addresses
  are rejected per row and reported back with their line number. Students are still created, and
  invites still sent, by the existing global import at `POST /teacher/students/import`.
- The same panel offers a **downloadable example CSV** built from the subject's active config:
  one row per variant actually declared in that subject, with mock e-mail addresses. A subject
  that declares no variants gets a header plus two mock rows with the variant column empty.
- What stays: Apply config, the test-student panel, feedback, and submission review.

## Capabilities

### New Capabilities
- `subject-enrollment`: enrolling existing students into a subject from a CSV of
  `email,variant`, the per-row validation and error reporting that goes with it, and the
  config-derived example CSV download.

### Modified Capabilities
- `subject-management`: the requirement that a "Remove Subject" button appear for the owner is
  withdrawn, and the existing "no manual subject-creation form" principle is widened into a
  general rule that the teacher UI exposes no affordance for mutating subject or assignment
  content — config re-apply is the only path.

## Impact

- **Templates**: `teacher_dashboard.html` (Analytics link), `teacher_subject.html` (edit,
  export, delete, create-assignment; gains the enrolment panel), `teacher_assignment.html`
  (quiz link, edit link, export).
- **Routes**: `api/routes/teacher_portal.py` — the existing per-subject import handler is
  reworked to the new CSV shape and error reporting; the per-subject template download is
  reworked to emit variant rows from config. Routes behind the removed buttons are untouched.
- **Data**: writes `subjects_students` and `students_assignments` rows only, using the same
  fan-out `enroll_student` already performs (`teacher_portal.py:1102`). No schema change.
- **Config reads**: the example CSV reads `assignments.<code>.variants` from the subject's
  active `SubjectPluginConfig`.
- **Docs**: `docs/teacher_journey_guide.md` and `docs/feature_catalog.md` describe the removed
  buttons and must be corrected. `docs/known_bugs.md:166` records the dead quiz editor; removing
  its link resolves part of that entry.
- **i18n**: new vocabulary keys for the enrolment panel; the keys behind removed buttons become
  unused.
