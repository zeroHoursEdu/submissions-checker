## 1. Database schema

- [x] 1.1 Add `SubjectStatus` StrEnum (`ACTIVE`, `DELETED`) to `db/models/enums.py`
- [x] 1.2 Update `Subject` model: add `owner_id` (nullable BIGINT FK → users), add `status` column (`SubjectStatus`, default `ACTIVE`, non-null), remove existing `unique=True` on `code`, add partial unique index on `code WHERE status='ACTIVE'` via `Index(..., postgresql_where=...)` in `__table_args__`
- [x] 1.3 Update `SubjectPluginConfig` model: add `zip_data` (LargeBinary, nullable) column to store the raw uploaded ZIP bytes; startup `PluginLoader` leaves this `None`
- [x] 1.4 Write Alembic migration: drop old `uq_subjects_code` unique constraint, add `owner_id` column, add `status` column with default `'ACTIVE'`, add partial unique index `uix_subjects_active_code`, add `zip_data BYTEA NULLABLE` to `subject_plugin_configs`

## 2. ConfigApplyService — plan computation

- [x] 2.1 Create `services/config_apply.py`; define `ConfigApplyPlan` dataclass with fields: `subject_action` (`"create"|"update"|"none"`), `subject_fields_changed` (list[str]), `new_s3_files` (list of (Path, s3_key) tuples), `removed_s3_keys` (list[str]), `assignments_to_create` (list[str]), `assignments_to_update` (list of (code, changed_fields) tuples), `assignments_to_delete` (list[str]); define `ApplyResult` dataclass with `changed: bool`, `subject_action: str`, `subject_name: str`
- [x] 2.2 Add `ConfigApplyService.__init__(self, storage: StorageService | None)` and `apply(zip_bytes: bytes, owner_id: int, db: AsyncSession) -> ApplyResult` entry point: validate size (≤50 MB, raise `ValueError`), extract ZIP to `TemporaryDirectory`, locate `config.yml` at root (raise `ValueError` if absent), parse YAML
- [x] 2.3 Add `_check_ownership` helper: look up subject by `subjectCode`; if found and `owner_id != current_owner_id`, raise `PermissionError`; return `(subject_or_none, previous_config_jsonb_or_empty_dict)`
- [x] 2.4 Add `_check_duplicate` helper: compute `sha256` of `zip_bytes`; query `SubjectPluginConfig` for matching `(subject_id, content_hash)`; if found, return `ApplyResult(changed=False, ...)`
- [x] 2.5 Add `_compute_plan(new_cfg, prev_cfg, subject, tmp_dir) -> ConfigApplyPlan` method:
  - Subject metadata diff: compare each of `name`, `description`, `github_repo`, `gridPicture`, `mainPicture` between new config and current DB values; collect changed field names
  - S3 image diff: for `gridPicture`/`mainPicture`, add to `new_s3_files` only if the filename differs from what was in `prev_cfg`; add old key to `removed_s3_keys` if key existed in previous config
  - Assignment diff: iterate new config's `assignments` dict; for existing assignments compare each field (`title`, `description`, `deadline`, `min_grade`, `max_grade`, config keys, `contentFiles`); collect only changed fields; new codes go to `assignments_to_create`; codes absent from new config go to `assignments_to_delete`
  - Content-file diff per assignment: add to `new_s3_files` only filenames not present in existing `sa.content_files`; add removed keys to `removed_s3_keys`
- [x] 2.6 Emit `logger.info("config_apply_plan", ...)` with all plan fields before any I/O

## 3. ConfigApplyService — execution

- [x] 3.1 Add `_execute_plan(plan, new_cfg, zip_bytes, sha256, subject, db, tmp_dir) -> ApplyResult` method
- [x] 3.2 Step 1 — upload only `plan.new_s3_files` to S3; collect `{s3_key: url}` mapping; raise on failure (no DB writes yet)
- [x] 3.3 Step 2-7 — open DB transaction: (a) update only `plan.subject_fields_changed` attributes on subject ORM object (create subject if `plan.subject_action == "create"`, setting `owner_id` and `status=ACTIVE`); (b) for each code in `assignments_to_create`: full insert; (c) for each `(code, changed_fields)` in `assignments_to_update`: fetch existing `SubjectsAssignment` and update only the listed fields; (d) for each code in `assignments_to_delete`: delete `SubjectsAssignment` row; (e) insert new `SubjectPluginConfig` row with incremented version, `content_hash=sha256`, `zip_data=zip_bytes`, `config=new_cfg`; await `db.commit()`
- [x] 3.4 Step 8 — best-effort S3 delete of `plan.removed_s3_keys`; log each failure with key name; do not raise
- [x] 3.5 For `assignments_to_create`: after inserting the `SubjectsAssignment`, create `StudentAssignment` rows for all currently enrolled students (same logic as existing `PluginLoader._upsert_assignment`)

## 4. Route integration

- [x] 4.1 Add `POST /teacher/subjects/apply-config` route to `teacher_portal.py`: read `UploadFile` bytes, call `ConfigApplyService(storage).apply(...)`, handle `ValueError` → 400, `PermissionError` → 403, generic → 500; on success redirect to `/teacher?apply_result=created` or `updated` or `unchanged`; on error redirect to `/teacher?apply_error=<encoded message>`
- [x] 4.2 Add `POST /teacher/subjects/{subject_id}/delete` route: fetch subject, check `owner_id` (raise 403 if mismatch), set `status = SubjectStatus.DELETED`, commit, redirect to `/teacher`
- [x] 4.3 Update teacher dashboard route query: add `.where(Subject.status == SubjectStatus.ACTIVE)` filter
- [x] 4.4 Pass `apply_result` and `apply_error` from request query params into the dashboard template context

## 5. Template updates

- [x] 5.1 Add "Upload Config" section to `templates/teacher_dashboard.html`: `<form enctype="multipart/form-data" method="POST" action="/teacher/subjects/apply-config">` with a file `<input accept=".zip">` and a submit button; place it in the page header area
- [x] 5.2 Add flash banners to `templates/teacher_dashboard.html`: success banner for `apply_result` values `created`/`updated`/`unchanged`; error banner for `apply_error`
- [x] 5.3 Add "Remove Subject" button to `templates/teacher_subject.html`: POST form to `/teacher/subjects/{{ subject.id }}/delete`; show only when `current_user.id == subject.owner_id`; include a JS `confirm()` call before submit

## 6. i18n vocabulary

- [x] 6.1 Add keys under `teacher:` in `i18n/uk.yml`: `upload_config_button`, `config_apply_created`, `config_apply_updated`, `config_apply_unchanged`, `config_apply_error_ownership`, `config_apply_error_invalid`, `remove_subject_button`, `remove_subject_confirm`
- [x] 6.2 Use `{{ vocab.teacher.<key> }}` for all strings added in steps 5.1–5.3
