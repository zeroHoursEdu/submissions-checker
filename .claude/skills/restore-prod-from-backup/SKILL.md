---
name: restore-prod-from-backup
description: DESTRUCTIVE restore of submissions-checker production (Postgres + MinIO) from a Google Drive backup. User-invoked only.
disable-model-invocation: true
---

# Restore production from a backup

Replaces the prod database AND bucket. Only when the user invoked this skill.

1. `scripts/ops/run-prod-backup.sh --list` — show stamps; ask which one (default `latest`). Say plainly that everything after that stamp is lost from the live system (it stays in `pre-restore/` on Drive).
2. Get an explicit "yes, restore <stamp>" from the user in this conversation.
3. `scripts/ops/restore-prod-from-backup.sh <stamp> --yes`
4. Verify: `curl -fsS https://<domain>/health/ready` (ask for the domain if unknown) and `scripts/ops/connect-to-prod-db.sh -c "select max(created_at) from submissions"` — report both.

If the restore fails, the database is unchanged (single transaction) and the app is restarted by the script; report the error output verbatim.

If the chosen stamp predates a migration that has since run, `pg_restore --clean` will
leave newer tables behind and the app's startup migration will then fail — see
`docs/deployment.md#backups-and-restore` ("Restoring across a migration boundary") for
the drop/recreate steps to run first in that case.

Restored the wrong stamp? Every restore snapshots what it overwrote to `pre-restore/`
first — see `docs/deployment.md#undoing-a-restore` for the manual steps to put it back,
as long as retention has not pruned it since.
