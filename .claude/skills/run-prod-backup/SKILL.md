---
name: run-prod-backup
description: Take, check or list submissions-checker production backups (Postgres + MinIO to Google Drive) — use before risky deploys/migrations, when asked whether backups work, or to find a restore point.
---

# Production backups

Backups run on prod every 6 hours when `COMPOSE_PROFILES` includes `backup`.

| Need | Command |
|---|---|
| Is the last backup recent? | `scripts/ops/run-prod-backup.sh --status` (non-zero exit if > 12h) |
| Backup right now | `scripts/ops/run-prod-backup.sh --now` |
| Restore points | `scripts/ops/run-prod-backup.sh --list` |
| Container log | `scripts/ops/run-prod-backup.sh --logs` (blocks; for the user) |

- Needs `PROD_SSH`/`PROD_DIR` (see `connect-to-prod-db`).
- "service not running" → backups are not enabled on prod; point the user to `docs/deployment.md#backups-and-restore`. Do not edit the prod `.env` yourself.
- Restoring is a different, user-only skill (`/restore-prod-from-backup`). Never restore from here.
