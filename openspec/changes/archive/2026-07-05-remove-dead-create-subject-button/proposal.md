## Why

`templates/teacher_dashboard.html` renders a "New Subject" button linking to `/teacher/subjects/create` — a route that doesn't exist (confirmed: zero matches for `subjects/create` anywhere in `src/submissions_checker/api/routes/`). Clicking it 404s. `templates/teacher_subject_form.html`, the form that dead route would have rendered, is likewise never rendered by any route. Subjects are, and per `subject-management`'s spec always have been, created and updated exclusively via the working ZIP-upload button right next to it. The dead button misleads teachers into thinking manual creation is supported.

## What Changes

- Remove the dead "New Subject" button from `templates/teacher_dashboard.html`.
- Delete the orphaned `templates/teacher_subject_form.html` (never rendered by any route).
- Add an explicit spec requirement stating the dashboard has no manual create-subject form — upload is the only creation path — so this doesn't silently regress back in.

## Capabilities

### Modified Capabilities
- `subject-management`: adds a requirement that the dashboard has no manual subject-creation
  form/button; ZIP upload is the sole path to create or update a subject.

## Impact

- `templates/teacher_dashboard.html` — dead button removed.
- `templates/teacher_subject_form.html` — deleted (orphaned template, never rendered).
- No route, model, or API changes — nothing referenced this dead path.
