#!/usr/bin/env bash
# Open psql on the production database over SSH — read-only unless --write.
set -euo pipefail
# shellcheck source=scripts/ops/_prod.sh
source "$(dirname "$0")/_prod.sh"

usage() {
  cat <<'EOF'
Usage: connect-to-prod-db.sh [--write] [--tunnel [LOCAL_PORT]] [-c SQL]

  (no args)       interactive psql inside the prod postgres container, READ-ONLY session
  -c SQL          run one statement and print the result (read-only unless --write)
  --write         allow writes (think twice; there is no undo besides a restore)
  --tunnel [PORT] forward localhost:PORT (default 15432) to prod postgres for a GUI client;
                  credentials are POSTGRES_USER/POSTGRES_PASSWORD from the prod .env

Needs PROD_SSH and PROD_DIR (env or ssh/prod.env).
EOF
}

mode=psql write=0 sql="" port=15432
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --write) write=1 ;;
    --tunnel) mode=tunnel; [[ "${2:-}" =~ ^[0-9]+$ ]] && { port="$2"; shift; } ;;
    -c) sql="${2:?-c needs SQL}"; shift ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
require_prod

if [[ "$mode" == tunnel ]]; then
  echo "Forwarding localhost:${port} -> prod postgres (Ctrl-C to close)"
  exec ssh -N -L "${port}:127.0.0.1:${POSTGRES_HOST_PORT:-5432}" "$PROD_SSH"
fi

opts="-c default_transaction_read_only=on"
[[ $write -eq 1 ]] && opts=""
# SC2016: this is a template for the REMOTE shell to expand, not this one.
# shellcheck disable=SC2016
inner='psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
if [[ -n "$sql" ]]; then
  prod_compose exec -T -e "PGOPTIONS=${opts}" postgres sh -c "${inner} -v ON_ERROR_STOP=1 -c $(printf %q "$sql")"
else
  SSH_TTY_FLAG=(-t)
  prod_compose exec -e "PGOPTIONS=${opts}" postgres sh -c "${inner}"
fi
