# Storage in MinIO/Postgres only, off-host backups to Google Drive — design

Date: 2026-09-28 · Branch: `storage-to-minio-and-drive-backups`

## Goal

All application data lives in exactly two stores — PostgreSQL and MinIO — and nothing
else on the host needs persisting. Both stores are backed up every 6 hours to Google
Drive (a cheap self-made off-host replica) and can be restored with one script. The app
is live in production, so every step must be non-breaking for an existing deployment.

## Decisions (made with the owner)

| # | Decision |
|---|---|
| D1 | Backups go to Drive; live volumes are never synced (a copy of a live Postgres data dir is not restorable). |
| D2 | Backups run every 6 hours at fixed clock times (00/06/12/18 Europe/Kyiv). |
| D3 | The backup container runs **only when configured** in `.env` (compose profile `backup`). A backup on the same disk as Postgres is pointless, so nothing is written to the prod disk. |
| D4 | Drive copies are **not encrypted** (owner's choice). Switching to an rclone `crypt` remote later needs no code change. |
| D5 | Ops logic lives in shebang scripts; Makefile targets are thin aliases; project skills call the scripts. |
| D6 | Skill and script share one verb-first kebab-case name (`connect-to-prod-db`, …). |

## 1. Current state (what is on local disk)

| Data | Where | Backed up today |
|---|---|---|
| Submission ZIPs | `uploads/` named volume, key `source_metadata.saved_as` | **No** |
| Unpacked subject trees | `plugins/<subjectCode>/` via `HOST_PLUGINS_DIR` | Not needed — the config ZIP is in `subject_plugin_configs.zip_data` (since 0018) |
| Pictures, content files, proctoring frames | MinIO | Yes (`mc mirror` to `BACKUP_DIR`) |
| Database | Postgres volume | Yes (`pg_dump` to `BACKUP_DIR`) |

## 2. Submission ZIPs move to MinIO

- Object key: `submissions/<saved_as>` (saved_as unchanged: `<sa_id>_<uuid>.zip`).
- **Write path** (`student_portal.submit_assignment`): upload bytes to MinIO before the DB
  flush/commit. A failed commit leaves an orphan object (harmless); the reverse order could
  leave a row pointing at nothing.
- **One read seam**: a new `services/submission_files.py` with
  `async def read_submission_zip(storage, saved_as) -> bytes | None` — MinIO first, then
  the legacy local file `uploads/<saved_as>` (path-traversal guarded), else `None`.
  All readers go through it:
  - `check_tasks` (both the sandbox path and the skip-tests path): bytes → temp dir → `safe_extract`.
  - upload-time similarity in `submit_assignment`.
  - teacher similarity report.
  - teacher ZIP download: `Response(bytes, media_type="application/zip")` with the same
    `Content-Disposition` filename as today. The bucket stays private.
- `similarity.compare_zip_files` / `token_set_for_zip` accept `Path | bytes`
  (`zipfile.ZipFile(io.BytesIO(...))`).
- **Backfill**: `python -m submissions_checker.cli.migrate_uploads` — for every `ZIP_UPLOAD`
  submission, uploads `uploads/<saved_as>` if the object is missing. Idempotent. Prints
  `copied / already present / missing locally` counts and lists subjects whose **latest**
  config has `zip_data IS NULL` (see §3). Exit code 0 even with missing files (it is a
  report), non-zero only on errors talking to MinIO/DB.
- **Phase 2 (a later release, not this branch)**: once the command reports 0 missing,
  remove the local fallback and the `uploads` volume. Documented in `docs/deployment.md`.

## 3. Subject trees are materialized from Postgres

- New `services/plugin_cache.py`:
  `materialize_plugin_tree(root: Path, subject_code: str, content_hash: str, zip_bytes: bytes) -> Path`
  - target `root/<subject_code>-<content_hash[:16]>/`; if it exists, return it.
  - else `safe_extract` into `root/.tmp-<uuid>`, chmod dirs 0755 / files 0644 (sandbox
    images run as non-root), `os.replace` to target (a concurrent loser discards its temp).
  - remove other `root/<subject_code>-*` directories (best effort).
- Root: new setting `plugin_cache_dir = "/tmp/subchk-plugins"`. `/tmp` is already
  bind-mounted same-path in prod compose, so the host daemon resolves the sandbox mount.
- `check_tasks`: if `config_record.zip_data` is present → materialize and use it.
  If NULL (configs applied before 0018) → legacy `host_plugins_dir or plugins_dir`
  `/<code>` if it is a directory, else fail validation with
  "Subject config has no stored archive — re-apply the subject config".
- `ConfigApplyService` stops extracting to `plugins_dir` (the `_extract_plugin_tree` call and
  the dedup self-heal branch go away; `plugins_dir` constructor argument removed).
- Compose: `HOST_PLUGINS_DIR` loses its `:?` guard; the plugins bind mount stays for this
  release (legacy fallback) as `${HOST_PLUGINS_DIR:-./plugins}`. Phase 2 removes it.
- Dev and e2e compose: switch S3 from localstack to MinIO (+ `minio-init`) so dev matches
  prod. e2e keeps working because configs are applied through the UI/API, which stores
  `zip_data`.

## 4. Backups

### Service
- `backup` gets `profiles: ["backup"]`. Enabled with `COMPOSE_PROFILES=backup`
  (comma-joined with `observability` if both). Env vars use `:-` defaults, not `:?`,
  because compose interpolates profiled services too.
- Image: alpine + `postgresql16-client` + `rclone` + `tzdata`; `mc` is dropped.
- Scheduling: busybox `crond`, `BACKUP_CRON` default `0 */6 * * *`, `TZ` default
  `Europe/Kyiv`. Logs go to container stdout.
- Startup check (fail fast, non-zero exit, clear message): `RCLONE_REMOTE` set, the
  mounted `rclone.conf` exists and `rclone lsd "$RCLONE_REMOTE"` succeeds.
- `rclone.conf` lives on the host (gitignored path, `RCLONE_CONFIG_FILE`, default
  `./docker/backup/rclone.conf`), mounted **read-write**: rclone writes refreshed Drive
  tokens back into it.
- MinIO is reached as an rclone S3 remote configured through env
  (`RCLONE_CONFIG_MINIO_TYPE=s3`, `…_PROVIDER=Minio`, `…_ENDPOINT=http://minio:9000`, keys).

### One run (`backup.sh once`)
Layout on the remote under `$RCLONE_REMOTE` (e.g. `gdrive:subchk`):
```
postgres/<stamp>.dump          pg_dump --format=custom
minio/current/…                 live mirror of the bucket
minio/deleted/<stamp>/…         objects removed/overwritten by that run
last-success                    stamp of the last fully successful run
```
1. `pg_dump` → `/tmp/<stamp>.dump` in the container → `rclone copyto` → delete local file.
2. `rclone sync minio:$BUCKET $REMOTE/minio/current --backup-dir $REMOTE/minio/deleted/<stamp>`.
   Dump first, objects second: every row in the dump has its object in the copy.
3. Only if 1 and 2 succeeded: `rclone delete --min-age ${BACKUP_RETENTION_DAYS:-30}d` on
   `postgres/` and `minio/deleted/` (then `rmdirs`), and write `last-success`.
   A failing run never prunes.
4. A run takes a lock (`flock`) so a manual run and the cron run cannot overlap.

### Restore (`restore-prod-from-backup.sh <stamp>|latest`)
The host-side script orchestrates; data steps run in the backup image via
`docker compose --profile backup run --rm backup restore …` (works whether or not the
profile is enabled in `.env`, so it also serves a fresh host):
1. Refuse without `--yes` or an interactive `yes` typed back.
2. Safety dump of the current DB uploaded to `$REMOTE/pre-restore/<now>.dump`.
3. Host script stops app replicas (`docker compose stop app`).
4. `pg_restore --clean --if-exists --no-owner` from the chosen dump.
5. `rclone sync $REMOTE/minio/current minio:$BUCKET` (objects are not point-in-time;
   extra orphans are harmless, `deleted/` holds anything newer that was removed).
6. Host script starts app and prints what was restored.

### Rollout on the existing prod host
Old backup container keeps running until the owner switches. Steps in
`docs/deployment.md`: create `rclone.conf` on a laptop (`rclone config`, needs a browser),
copy to host → set `COMPOSE_PROFILES`, `RCLONE_REMOTE` → `build backup` +
`up -d --force-recreate backup` → `run-prod-backup.sh --now` → confirm on Drive →
only then delete the old `BACKUP_DIR` contents by hand.

## 5. Ops scripts, Makefile, skills

### Scripts (`scripts/ops/`, `#!/usr/bin/env bash`, `set -euo pipefail`, `--help`)
| Script | Does | Destructive |
|---|---|---|
| `connect-to-prod-db.sh [--write] [-- psql args]` | SSH tunnel to prod `127.0.0.1:$POSTGRES_HOST_PORT`, opens psql; read-only session (`default_transaction_read_only=on`) unless `--write` | no (unless `--write`) |
| `run-prod-backup.sh [--now\|--status\|--list]` | over SSH: run a backup now / age of `last-success` / list dumps | no |
| `restore-prod-from-backup.sh <stamp\|latest> [--yes]` | §4 restore | **yes** |

Prod host comes from `PROD_SSH` (e.g. `root@1.2.3.4`) and `PROD_DIR` (compose dir on the
host), read from the environment or the gitignored `ssh/prod.env`. No address in git.

### Makefile
- Every Python tool via `uv run --frozen --extra dev`.
- `install` → `uv sync --frozen --extra dev`; delete `venv`, `activate`, `test-watch`, `dev`,
  `shell`, `build`, `rebuild`, `ps`; merge `e2e`/`e2e-headed` (`HEADED=1`); full `.PHONY`.
- `dev_setup.sh` uses `uv sync --frozen --extra dev`.
- `make help` grouped: Dev / Test / Quality / E2E / Observability / Prod ops.
- New: `prod-db`, `prod-backup`, `prod-backup-status`. No make target for restore.
- README: fix "make up starts prometheus/grafana/alloy".
- New `docs/commands.md`: every target and script — what, when, prerequisites, destructive?

### Skills (`.claude/skills/<name>/SKILL.md`, tracked)
- `connect-to-prod-db` — when to use, calls the script, read-only default, never `--write`
  without the user asking.
- `run-prod-backup` — run/status/list.
- `restore-prod-from-backup` — `disable-model-invocation: true`; user-only.

## Testing

- Unit: `plugin_cache` (materialize, reuse, concurrent-safe replace, stale cleanup, perms);
  `submission_files` (S3 hit, local fallback, traversal guard, none); similarity with bytes.
- Functional: submit stores object in MinIO (fake storage), teacher download serves from
  storage, similarity uses storage; check task with `zip_data` uses the cache, NULL
  `zip_data` falls back / fails with the message; config apply no longer writes `plugins/`.
- `migrate_uploads`: copies missing, skips present, reports missing, lists NULL-zip subjects.
- Shell: `shellcheck` on all scripts; `bash -n`; backup/restore exercised once manually
  against the dev stack with a local rclone remote (`RCLONE_REMOTE=/tmp/…` local path).
- Full suite + ruff + mypy green before merge.

## Out of scope
Phase 2 removal of the local fallbacks and `uploads`/plugins mounts; encryption; WAL/PITR;
Grafana alert on backup age.
