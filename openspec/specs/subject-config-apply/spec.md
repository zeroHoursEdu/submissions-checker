# Spec: Subject Config Apply

## Purpose

Defines the behaviour of the ZIP-based subject configuration apply endpoint, including upload validation, diff-driven execution, S3/DB ordering guarantees, and config versioning.

## Requirements

### Requirement: Teacher can upload a ZIP config to create or update a subject

The system SHALL provide a `POST /teacher/subjects/apply-config` endpoint that accepts a multipart ZIP file upload from an authenticated teacher.

The ZIP file SHALL contain at least a `config.yml` at its root with a `subjectCode` field.

The system SHALL extract the ZIP to a temporary directory, parse `config.yml`, and determine whether the subject identified by `subjectCode` already exists in the database with `status = ACTIVE`.

The endpoint SHALL be synchronous — the HTTP response is returned only after the full apply is complete (S3 uploads, DB commit, S3 cleanup).

#### Scenario: New subject created from ZIP
- **WHEN** a teacher uploads a valid ZIP whose `subjectCode` does not match any `ACTIVE` subject
- **THEN** a new `Subject` is inserted with `status = ACTIVE` and `owner_id` set to the uploading teacher's user ID, and the browser receives a redirect with a "created" success indicator in the same request

#### Scenario: Existing subject updated by its owner
- **WHEN** a teacher uploads a valid ZIP whose `subjectCode` matches an `ACTIVE` subject and `subject.owner_id == current_user.id`
- **THEN** the subject metadata and assignments are updated according to the field-level diff plan, and the browser receives a redirect with an "updated" success indicator

#### Scenario: Non-owner teacher attempts config apply
- **WHEN** a teacher uploads a valid ZIP whose `subjectCode` matches an `ACTIVE` subject and `subject.owner_id != current_user.id`
- **THEN** the system SHALL return HTTP 403; no DB or S3 changes are made

#### Scenario: ZIP has no config.yml
- **WHEN** a teacher uploads a ZIP that contains no `config.yml` at the root
- **THEN** the system SHALL return HTTP 400; no changes are made

#### Scenario: ZIP exceeds size limit
- **WHEN** the uploaded file exceeds 50 MB
- **THEN** the system SHALL return HTTP 413; no extraction occurs

### Requirement: Config apply is diff-driven with a two-phase check

**Phase 1 — config-level check**: compute SHA-256 of the uploaded ZIP bytes. If `SubjectPluginConfig` already has a row with the same `content_hash` for this subject, skip execution entirely and respond with `unchanged`.

**Phase 2 — field-level diff** (only when hash differs): compare the new config against the latest stored `SubjectPluginConfig.config` JSONB field-by-field. Only fields, assignments, and S3 files that have actually changed SHALL be written or uploaded.

The system SHALL NOT overwrite any DB field or upload any S3 file whose value is identical to the current state.

The system SHALL emit a single structured log entry at INFO level with `event=config_apply_plan` describing the full plan (changed subject fields, assignments to create/update/delete, S3 files to add/remove) before any I/O begins.

#### Scenario: Plan logged before execution
- **WHEN** a valid config apply begins
- **THEN** a log line with `event=config_apply_plan` and all plan fields is emitted before any S3 or DB write

#### Scenario: Unchanged config skipped without any writes
- **WHEN** the uploaded ZIP has the same SHA-256 hash as the latest `SubjectPluginConfig.content_hash` for this subject
- **THEN** the system SHALL skip all I/O, log `event=config_apply_unchanged`, and respond with an `unchanged` indicator — no DB row is inserted, no S3 upload occurs

#### Scenario: Only changed fields are written
- **WHEN** a ZIP is uploaded that changes only an assignment's `deadline` field
- **THEN** only `subjects_assignments.deadline` is updated; all other assignment columns and subject columns are left untouched

#### Scenario: Only new S3 files are uploaded
- **WHEN** a new config references the same `gridPicture` filename as the current config
- **THEN** no S3 upload is performed for that image

### Requirement: Apply execution follows S3-first, then DB-transaction, then S3-cleanup order

The system SHALL execute the apply-plan in this strict sequence:
1. Upload only the `new_s3_files` listed in the plan to S3; collect returned public URLs
2. Open a DB transaction
3. Apply subject metadata changes (only changed fields)
4. Apply assignment creates, field-level updates, and deletes as per the plan
5. Insert a new `SubjectPluginConfig` row: incremented `version`, `content_hash`, `config` JSONB, `zip_data` (raw bytes)
6. Commit the DB transaction
7. Best-effort delete `removed_s3_keys` from S3; log any failures but do not fail the response

#### Scenario: S3 upload fails before DB write
- **WHEN** an S3 upload fails during step 1
- **THEN** the system SHALL abort, return HTTP 500, and make no DB changes

#### Scenario: DB commit fails after S3 upload
- **WHEN** the DB transaction fails in step 6
- **THEN** orphan S3 objects may remain; the system SHALL return HTTP 500 and log the orphan S3 keys

### Requirement: ZIP blob and version are stored in `SubjectPluginConfig`

The existing `SubjectPluginConfig` table (which already stores `version`, `content_hash`, `config` JSONB) SHALL be extended with a `zip_data` (LargeBinary, nullable) column.

Each successful apply via ZIP upload SHALL insert a `SubjectPluginConfig` row with `zip_data` set to the raw ZIP bytes. Rows created by the startup `PluginLoader` SHALL have `zip_data = NULL`.

#### Scenario: Version increments on each successful apply
- **WHEN** a subject already has N config versions and a new apply succeeds
- **THEN** a new `SubjectPluginConfig` row is inserted with `version = N + 1`

#### Scenario: Duplicate ZIP not re-stored
- **WHEN** the uploaded ZIP has the same SHA-256 as an existing `SubjectPluginConfig.content_hash` for this subject
- **THEN** no new row is inserted and the response indicates `unchanged`

### Requirement: ZIP upload extracts the full plugin tree to disk

A successful config apply SHALL extract the entire uploaded ZIP archive (not only `config.yml`
and the images/content files already referenced by the diff plan) to `plugins_dir/<subjectCode>/`
on disk, atomically replacing any previously extracted tree for that subject. This makes the
`config-apply` endpoint sufficient on its own to make a subject checkable — the extracted tree is
exactly what `check_tasks.py` mounts read-only into the sandbox at check-execution time.

The extraction SHALL happen after the DB transaction in the apply sequence commits successfully,
and SHALL be atomic from the perspective of any concurrently running check (a check reading
`plugins_dir/<subjectCode>/` at any point during the swap SHALL see either the complete previous
tree or the complete new tree, never a partial one).

The duplicate-ZIP short-circuit (matching `content_hash`) SHALL also verify that
`plugins_dir/<subjectCode>/` exists on disk before skipping extraction; if the hash matches but
the directory is missing (e.g. a prior extraction failed after its DB commit), the system SHALL
still perform the extraction rather than treating the apply as a no-op.

#### Scenario: New subject's checker code is extracted

- **WHEN** a teacher uploads a ZIP creating a new subject, and the DB transaction commits
- **THEN** the full ZIP contents (checker scripts, fixtures, `config.yml`, everything) are
  extracted to `plugins_dir/<subjectCode>/`, and a check run against that subject can resolve its
  `/plugin` mount without any manual file placement

#### Scenario: Re-upload replaces stale files

- **WHEN** a teacher re-uploads a ZIP for an existing subject that removes an assignment's
  `check.py` present in the previous version
- **THEN** after the apply completes, `plugins_dir/<subjectCode>/` no longer contains that
  removed file — the on-disk tree matches the new ZIP exactly, not a merge of old and new

#### Scenario: Concurrent check sees a consistent tree

- **WHEN** a check task resolves `plugin_dir` for a subject at the same moment a new apply is
  swapping that subject's extracted tree
- **THEN** the check task SHALL see either the fully-old or fully-new tree, never a directory
  missing files or containing a mix of both versions

#### Scenario: Retry after a failed extraction re-extracts instead of no-op'ing

- **WHEN** an apply's DB transaction commits but the subsequent disk extraction fails or is
  interrupted, and the same ZIP is uploaded again
- **THEN** the system SHALL detect that `plugins_dir/<subjectCode>/` is missing despite the
  matching `content_hash`, and SHALL perform the extraction rather than skipping it as unchanged

### Requirement: Config apply persists AI-review and grading blocks

The subject config-apply service SHALL include the per-assignment `ai_review` and `grading` blocks in the set of assignment config fields it persists to `subjects_assignments.config` and compares during its field-level diff. A change to either block SHALL be detected as a change to the assignment's config so the assignment row is updated, and an unchanged block SHALL NOT trigger a spurious update.

#### Scenario: AI-review block persisted on apply

- **WHEN** a teacher uploads a ZIP whose assignment defines an `ai_review` block
- **THEN** the `ai_review` block is stored under `subjects_assignments.config` for that assignment

#### Scenario: Grading block persisted on apply

- **WHEN** a teacher uploads a ZIP whose assignment defines a `grading` block
- **THEN** the `grading` block is stored under `subjects_assignments.config` for that assignment

#### Scenario: Change to grading weights is diffed

- **WHEN** a subsequent upload changes only an assignment's `grading.code_weight`
- **THEN** the field-level diff detects the assignment config changed and updates that assignment, without reporting unrelated assignments as changed

#### Scenario: Unchanged blocks cause no update

- **WHEN** a re-upload leaves `ai_review` and `grading` byte-identical
- **THEN** neither block causes the assignment to be reported as changed
