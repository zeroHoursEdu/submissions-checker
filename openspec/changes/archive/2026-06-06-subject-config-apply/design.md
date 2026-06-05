## Context

Subject configuration is currently loaded from the server filesystem at startup via `PluginLoader`. Teachers have no in-app way to create or update subjects. The `PluginLoader` already handles all upsert logic (parse config.yml, upload images to S3, upsert assignments, upload content files). This design reuses that logic inside a new request-scoped `ConfigApplyService` that adds: ZIP extraction, field-level diffing, a structured apply-plan with logging, and ownership enforcement.

The `subjects` table has a `code` column (unique) but no `owner_id` or `status`.

`SubjectPluginConfig` already exists and already handles config versioning: it stores `version` (auto-incremented per subject), `content_hash` (SHA-256 for deduplication), and `config` (JSONB). We extend it with a `zip_data` (LargeBinary) column to also persist the raw ZIP bytes. No new table is needed.

## Goals / Non-Goals

**Goals:**
- ZIP upload endpoint callable from the teacher UI; response is synchronous — the teacher sees success/error in the same page load
- Ownership: subject creator is the sole modifier/deleter
- Soft-delete (`status = DELETED`) with partial unique index on `(code)` where `status = ACTIVE`
- Config versioning: extend existing `SubjectPluginConfig` with `zip_data` column; deduplication by SHA-256 already implemented
- Field-level delta apply: only write DB rows/fields and upload S3 objects that actually changed
- Logged apply-plan: structured log entry before execution so the operation is auditable
- Safe execution order: S3 uploads first → DB transaction → S3 purge of old keys

**Non-Goals:**
- Background/async processing — the apply must complete within the HTTP request
- Real-time progress streaming
- Rollback of S3 uploads on DB failure (orphaned objects are acceptable; re-uploading the same file overwrites them)
- Multi-owner or ACL-style sharing
- Restoring a `DELETED` subject through the UI

## Decisions

### D1 — Reuse `PluginLoader` logic in a new `ConfigApplyService`

`PluginLoader.load_all` walks a filesystem directory. A new `ConfigApplyService` in `services/config_apply.py` will:
1. Accept an in-memory ZIP (`bytes`)
2. Extract it to a `tempfile.TemporaryDirectory`
3. Use adapted internal methods (matching those in `PluginLoader`) for upsert logic, but driven by a computed diff rather than unconditional upsert

Rather than call `PluginLoader` directly (which scans a whole directory and does blind upserts), `ConfigApplyService` encapsulates a single-config apply with true delta semantics.

### D2 — Two-phase diff: config-level then field-level

**Phase 1 — config-level check**: compute SHA-256 of the uploaded ZIP bytes. If `SubjectPluginConfig` already has a row with the same `content_hash` for this subject, skip execution entirely (`ApplyResult(changed=False)`).

**Phase 2 — field-level diff** (only if config hash differs). Compare the new config against the latest `SubjectPluginConfig.config` JSONB row-by-row and field-by-field:

- **Subject metadata** (`name`, `description`, `github_repo`, `gridPicture`, `mainPicture`): compare each field; only set the attribute and mark the ORM object dirty if the value differs from what's in the DB.
- **Assignments**: for each assignment code in the new config:
  - If the assignment doesn't exist in DB → create it (full insert)
  - If it exists → compare each field (`title`, `description`, `deadline`, `min_grade`, `max_grade`, `config` JSONB, `contentFiles`). Only update fields that differ.
- **Assignment removals**: assignment codes in the previous JSONB config that are absent from the new config → delete the `SubjectsAssignment` row (which cascades to `StudentAssignment`)
- **S3 files**: an image or content file is only uploaded if its S3 key is not already referenced in the previous config's stored URLs. An S3 key is only purged if it was referenced in the previous config and is absent from the new one.

The diff produces a `ConfigApplyPlan` dataclass (fields below). The plan is logged before any I/O begins.

```python
@dataclass
class ConfigApplyPlan:
    subject_action: Literal["create", "update", "none"]
    subject_fields_changed: list[str]        # field names that differ
    new_s3_files: list[tuple[Path, str]]     # (local_path, s3_key)
    removed_s3_keys: list[str]
    assignments_to_create: list[str]         # assignment codes
    assignments_to_update: list[tuple[str, list[str]]]  # (code, changed_fields)
    assignments_to_delete: list[str]
```

### D3 — Execution order: S3-first, then DB transaction, then S3 cleanup

Rationale: DB transactions are ACID; S3 is not. If we write to DB first and S3 upload fails, the DB references broken URLs. Reversing the order means a failed DB write leaves orphan S3 objects (benign — re-uploading overwrites). Old S3 keys are purged *after* the DB transaction commits (best-effort; failures are logged but do not roll back).

Sequence:
1. Upload `new_s3_files` → collect `{s3_key: url}` mapping
2. Open DB transaction
3. Apply subject metadata changes (only changed fields)
4. Apply assignment creates/updates (only changed fields per assignment)
5. Delete removed assignments
6. Insert new `SubjectPluginConfig` row (incremented `version`, `content_hash`, `config` JSONB, `zip_data`)
7. Commit
8. Best-effort delete `removed_s3_keys` from S3; log any failures

### D4 — Partial unique index for `(code, ACTIVE)` — replace existing constraint

`UniqueConstraint("code", name=...)` is currently on `subjects`. We drop it and replace with a `partial unique index` on `(code)` `WHERE status = 'ACTIVE'`. PostgreSQL supports this natively; SQLAlchemy expresses it via `Index(..., postgresql_where=...)`. This allows unlimited `DELETED` rows with the same code.

### D5 — `SubjectPluginConfig` extended with `zip_data`, no new table

Adding `zip_data LargeBinary NULLABLE` to `SubjectPluginConfig` stores the raw ZIP alongside the existing JSONB config and version counter. The startup `PluginLoader` continues to work unchanged (it leaves `zip_data = None`). ZIP uploads through the UI set `zip_data` to the raw bytes.

### D6 — Ownership stored as `owner_id` on `subjects`, nullable for legacy rows

Existing subjects created by the startup loader have no teacher owner. `owner_id` is `NULLABLE`. The UI shows the "apply config" and "remove subject" buttons only when `current_user.id == subject.owner_id`. The backend enforces it with a 403 on mismatch.

### D7 — Synchronous HTTP response

The `POST /teacher/subjects/apply-config` route awaits `ConfigApplyService.apply()` inline and redirects (or renders) immediately with the result. No background task, no polling. The apply is expected to complete in seconds for typical subject configs.

## Risks / Trade-offs

- **Large ZIP** → Enforce 50 MB limit before extraction.
- **Orphan S3 objects after DB failure** → Acceptable; logged with key names for manual cleanup.
- **Temp dir on process crash** → `tempfile.TemporaryDirectory` as context manager handles normal exits.
- **Removing existing `UNIQUE` constraint on `subjects.code`** → Single Alembic revision: drop old constraint, add partial index.
- **Large `zip_data` in `SubjectPluginConfig`** → ZIP files for typical subject configs are small (< a few MB). Acceptable for now; can be moved to S3 if needed later.

## Migration Plan

1. New Alembic revision:
   - Add `owner_id BIGINT REFERENCES users(id) NULLABLE` to `subjects`
   - Add `status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'` to `subjects`
   - Drop `uq_subjects_code` unique constraint
   - Add partial unique index `uix_subjects_active_code` on `subjects(code)` WHERE `status = 'ACTIVE'`
   - Add `zip_data BYTEA NULLABLE` column to `subject_plugin_configs`
2. Existing subjects remain `ACTIVE` with `owner_id = NULL` — no data loss.
3. Existing `SubjectPluginConfig` rows have `zip_data = NULL` — no data loss.

## Open Questions

- Should deleted subjects be visible to admins in a special view? (Not in scope — admin can query directly.)
