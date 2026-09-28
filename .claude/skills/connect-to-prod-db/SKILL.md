---
name: connect-to-prod-db
description: Query the production PostgreSQL of submissions-checker (read-only by default) — use when the user asks about live/prod data, counts, a specific student/submission in production, or to inspect prod schema state.
---

# Connect to the production database

Run `scripts/ops/connect-to-prod-db.sh -c "<SQL>"` — one statement, output printed.
The session is READ-ONLY (`default_transaction_read_only=on`).

- Needs `PROD_SSH` and `PROD_DIR` (env or `ssh/prod.env`, template `scripts/ops/prod.env.example`). If missing, ask the user for them; never guess a host.
- Prefer narrow queries with `LIMIT`; the host has 2GB RAM and serves students.
- Treat results as personal data: summarise, do not paste whole tables of students.
- `--write` exists. Use it only when the user explicitly asked for that specific change in this conversation, and suggest `scripts/ops/run-prod-backup.sh --now` first.
- `--tunnel` is for the user's GUI client; do not start it yourself (it blocks).
