#!/usr/bin/env bash
# Trigger or inspect production backups (the `backup` service must be enabled on prod).
set -euo pipefail
# shellcheck source=scripts/ops/_prod.sh
source "$(dirname "$0")/_prod.sh"

usage() {
  cat <<'EOF'
Usage: run-prod-backup.sh [--now | --status | --list | --logs]

  --now     take a backup right now (e.g. before a risky deploy or migration)
  --status  when the last successful backup ran; exits non-zero if older than 12h (default)
  --list    stamps of available database dumps, oldest first (use one with restore)
  --logs    follow the backup container's log

Backups run by themselves every 6 hours; this is for the moments in between.
Needs PROD_SSH and PROD_DIR (env or ssh/prod.env).
EOF
}

action="${1:---status}"
case "$action" in
  -h|--help) usage; exit 0 ;;
  --now) cmd=(exec -T backup backup.sh once) ;;
  --status) cmd=(exec -T backup backup.sh status) ;;
  --list) cmd=(exec -T backup backup.sh list) ;;
  --logs) cmd=(--profile backup logs -f --tail 100 backup) ;;
  *) echo "unknown argument: $action" >&2; usage >&2; exit 2 ;;
esac
require_prod
prod_compose "${cmd[@]}" || {
  echo "Failed. If the service is not running, enable it: COMPOSE_PROFILES=backup in the prod .env (docs/deployment.md#backups-and-restore)." >&2
  exit 1
}
