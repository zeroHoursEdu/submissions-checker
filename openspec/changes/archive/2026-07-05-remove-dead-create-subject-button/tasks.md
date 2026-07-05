## 1. Remove dead UI

- [x] 1.1 In `templates/teacher_dashboard.html`, remove the `<a href="/teacher/subjects/create">` button (~line 63-69).
- [x] 1.2 Delete `templates/teacher_subject_form.html`.
- [x] 1.3 Check `i18n/uk.yml` for a `vocab.teacher.new_subject` key used only by the removed button; remove it if it has no other callers.

## 2. Verification

- [x] 2.1 Grep the codebase to confirm nothing else references `teacher_subject_form.html` or `/teacher/subjects/create`.
- [x] 2.2 Run the functional test suite for teacher_dashboard/portal pages to confirm no regression.
