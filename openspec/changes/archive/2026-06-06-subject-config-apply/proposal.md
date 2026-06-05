## Why

Teachers currently configure subjects by placing config directories on the server filesystem — there is no in-app way to upload or update a subject's configuration. This blocks remote workflows and prevents proper ownership enforcement. Adding a ZIP upload flow lets each teacher manage their own subjects from the UI without requiring server access.

## What Changes

- Add `owner_id` (FK → users) and `status` (`ACTIVE` | `DELETED`) columns to `subjects` table
- Add a partial unique constraint: only one active subject per `code` (multiple `DELETED` rows are allowed)
- Add `SubjectConfigVersion` table to store the uploaded ZIP blob alongside the parsed JSONB config and a version counter per subject
- Expose a `POST /teacher/subjects/apply-config` endpoint that accepts a ZIP upload, extracts it, diffs it against the current active config, generates a structured apply-plan, and executes it in the correct sequence (S3 first, then DB transaction, then S3 cleanup)
- Add ownership enforcement: only the teacher who created a subject (its `owner_id`) may apply a new config or soft-delete it; other teachers get a 403 with a UI-rendered error message
- Soft-delete: the owner can mark a subject `DELETED`; a "Remove subject" button appears on the subject detail page; deleted subjects are hidden from the dashboard
- Teacher dashboard gets an "Upload Config" button (ZIP file picker) that POSTs to the new endpoint
- Log the computed apply-plan (new S3 files, DB upserts, DB removals, old S3 keys to purge) before execution so the operation is auditable

## Capabilities

### New Capabilities

- `subject-config-apply`: ZIP-driven subject create/update flow with diff plan, ownership checks, and soft-delete

### Modified Capabilities

- `subject-management`: subjects table gains `owner_id`, `status`, and the unique-active-code constraint; dashboard filters to show only `ACTIVE` subjects; subject detail page gains a soft-delete button for the owner

## Impact

- **DB migration**: `subjects` table (`owner_id`, `status`, partial unique index); new `subject_config_versions` table (stores raw ZIP bytes + JSONB config + version)
- **`services/plugin_loader.py`**: diff logic extracted/extended into a new `ConfigApplyService`
- **`api/routes/teacher_portal.py`**: new upload route; dashboard query adds `status = ACTIVE` filter; subject detail adds soft-delete route
- **`templates/teacher_dashboard.html`**: Upload Config button
- **`templates/teacher_subject.html`**: Remove Subject button (owner only)
- **S3**: new keys under `subjects/<code>/config-versions/<version>/`; old keys removed after successful transaction
- **`db/models/subject.py`**: two new columns, updated `__table_args__`
- **New model** `db/models/subject_config_version.py`
- **New service** `services/config_apply.py`
