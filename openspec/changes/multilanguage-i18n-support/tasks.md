## 1. Vocabulary file

- [x] 1.1 Create `i18n/` directory at project root
- [x] 1.2 Create `i18n/uk.yml` with `_meta` section (`label: Українська`, `code: uk`) and nested sections for all UI text (`nav`, `auth`, `student`, `teacher`, `admin`, `analytics`, `feedback`, `notifications`, `common`)
- [x] 1.3 Audit every `templates/*.html` file to extract all hardcoded user-visible strings and add them as keys under the appropriate section in `uk.yml`

## 2. Core i18n module

- [x] 2.1 Create `src/submissions_checker/core/i18n.py` with `load_vocabularies(path: Path)` function that scans `*.yml` files, reads each into a dict keyed by language code, and builds `AVAILABLE_LANGUAGES` list from `_meta.label` values
- [x] 2.2 Add `get_vocab(lang_cookie: str | None) -> dict` in `i18n.py` that resolves the active vocabulary dict based on cookie value, falling back to first registered language

## 3. Shared templates module

- [x] 3.1 Create `src/submissions_checker/core/templates.py` with a single `Jinja2Templates` instance pointing to `"templates"`, configured with `DebugUndefined` in development
- [x] 3.2 Add `render(request, template_name, context)` helper in `templates.py` that calls `get_vocab` with the `lang` cookie, merges `vocab` and `available_languages` into the context (without overwriting caller-supplied keys), then returns `templates.TemplateResponse`

## 4. Startup integration

- [x] 4.1 Call `load_vocabularies(Path("i18n/"))` in `main.py` lifespan startup block (before scheduler, after migrations); log the number of discovered languages

## 5. Language switch endpoint

- [x] 5.1 Create `src/submissions_checker/api/routes/i18n.py` with `POST /set-language` route: validate `lang` form field against registered codes, set `lang` cookie (`max_age=31536000`, `samesite="lax"`, `httponly=True`, `path="/"`), redirect to `Referer` header or `/`; return 400 for unknown codes
- [x] 5.2 Include `i18n.router` in `create_app()` inside `main.py`

## 6. Route files refactor

- [x] 6.1 Update `api/routes/auth.py`: import `render` from `core.templates`, remove local `Jinja2Templates` instantiation, replace all `templates.TemplateResponse` calls with `render`
- [x] 6.2 Update `api/routes/student_portal.py` the same way
- [x] 6.3 Update `api/routes/teacher_portal.py` the same way
- [x] 6.4 Update `api/routes/admin.py` the same way
- [x] 6.5 Update `api/routes/analytics.py` the same way
- [x] 6.6 Update `api/routes/notifications.py` the same way
- [x] 6.7 Update `api/routes/student_quiz.py` the same way
- [x] 6.8 Update `api/routes/feedback.py` the same way

## 7. Template string replacement

- [x] 7.1 Replace all hardcoded English strings in `templates/base.html` with `{{ vocab.<section>.<key> }}` references
- [x] 7.2 Replace strings in `templates/login.html`, `templates/forgot_password.html`, `templates/reset_password.html`
- [x] 7.3 Replace strings in `templates/student_select.html`, `templates/student_summary.html`, `templates/student_quiz.html`, `templates/student_quiz_result.html`
- [x] 7.4 Replace strings in `templates/teacher_dashboard.html`, `templates/teacher_subject.html`, `templates/teacher_subject_form.html`, `templates/teacher_assignment.html`, `templates/teacher_assignment_form.html`, `templates/teacher_students.html`, `templates/teacher_add_student.html`, `templates/teacher_submission_review.html`, `templates/teacher_quiz_editor.html`, `templates/teacher_feedback_view.html`
- [x] 7.5 Replace strings in `templates/admin_dashboard.html`, `templates/admin_users.html`, `templates/admin_create_teacher.html`, `templates/admin_audit.html`
- [x] 7.6 Replace strings in `templates/analytics_dashboard.html`, `templates/analytics_student.html`, `templates/analytics_fraud.html`
- [x] 7.7 Replace strings in `templates/notifications.html`, `templates/student_notification_preferences.html`
- [x] 7.8 Replace strings in `templates/feedback_form.html`, `templates/feedback_thank_you.html`, `templates/feedback_not_found.html`, `templates/feedback_already_submitted.html`
- [x] 7.9 Replace strings in `templates/assignments.html`, `templates/subjects.html`

## 8. Language selector UI

- [x] 8.1 Add language selector form to `templates/base.html` header: `<form method="POST" action="/set-language">` with `<select name="lang">` populated via `{% for lang in available_languages %}`, hidden `<input name="next" value="{{ request.url }}">`, shown only when `available_languages | length > 1`
