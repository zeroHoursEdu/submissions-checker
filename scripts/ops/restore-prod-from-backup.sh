#!/usr/bin/env bash
# DESTRUCTIVE: replace the production database and bucket with a backup.
set -euo pipefail
# shellcheck source=scripts/ops/_prod.sh
source "$(dirname "$0")/_prod.sh"

usage() {
  cat <<'EOF'
Usage: restore-prod-from-backup.sh <STAMP|latest> [--yes]

Replaces the production database AND the MinIO bucket with backup STAMP
(see: run-prod-backup.sh --list). Steps: stop app -> save current DB to
pre-restore/<now>.dump on the remote -> pg_restore in one transaction ->
sync bucket -> start app. The app is started again even if the restore fails.

Works on a fresh host too (the backup profile need not be enabled).
--yes skips the typed confirmation. Needs PROD_SSH and PROD_DIR.
EOF
}

stamp="" yes=0
for a in "$@"; do
  case "$a" in
    -h|--help) usage; exit 0 ;;
    --yes) yes=1 ;;
    -*) echo "unknown argument: $a" >&2; usage >&2; exit 2 ;;
    *) stamp="$a" ;;
  esac
done
[[ -n "$stamp" ]] || { usage >&2; exit 2; }
require_prod

if [[ $yes -ne 1 ]]; then
  echo "This REPLACES the database and bucket on ${PROD_SSH} with backup '${stamp}'."
  read -r -p "Type 'restore' to continue: " answer
  [[ "$answer" == "restore" ]] || { echo "aborted"; exit 1; }
fi

prod_compose stop app
trap 'prod_compose up -d app' EXIT
prod_compose --profile backup run --rm -e RESTORE_CONFIRM=yes backup restore "$stamp"
echo "Restore of ${stamp} finished; starting app."
