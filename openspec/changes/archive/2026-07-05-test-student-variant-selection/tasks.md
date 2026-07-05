## 1. Route

- [x] 1.1 In `src/submissions_checker/api/routes/teacher_portal.py`'s `provision_test_student`, add `request: Request` to the signature and change the assignment query to select `SubjectsAssignment.id, .code, .config` (not just `.id`).
- [x] 1.2 After fetching, call `form = await request.form()`. For each assignment row: compute `variants = sa.config.get("variants") or {}`. If `variants` is non-empty, read `submitted = form.get(f"variant_{sa.code}")`; if `submitted in variants`, use it; otherwise (missing/invalid) default to `sorted(variants)[0]`. If `variants` is empty, leave `variant` as `None`. Set this on the created `StudentAssignment(..., variant=chosen)`.

## 2. Template

- [x] 2.1 In `templates/teacher_subject.html`'s "Create Test Student" form (~line 196-201), before the submit button, loop over `assignments` and for each `a` with a non-empty `a.config.get('variants')`, render a labeled `<select name="variant_{{ a.code }}">` with one `<option>` per sorted variant key. For assignments where `a.config.get('variants_required')` is true, do not include a blank option (so the browser's default first-option selection is itself a valid variant). For assignments where variants are configured but not required, include a leading blank "No variant" option.

## 3. Tests

- [x] 3.1 Add/extend a functional test: provisioning a test student for a subject with a `variants_required` assignment, without touching the form, results in `StudentAssignment.variant` set to the first sorted variant key.
- [x] 3.2 Add a test asserting an explicitly submitted valid variant is persisted as-is.
- [x] 3.3 Add a test asserting an invalid/tampered submitted variant falls back to the first sorted key rather than being stored verbatim.
- [x] 3.4 Add a test asserting an assignment with no `variants` configured still gets `variant = None`, unchanged from current behavior.
- [x] 3.5 Run the functional test suite for teacher_portal/test-student routes to confirm no regression.
