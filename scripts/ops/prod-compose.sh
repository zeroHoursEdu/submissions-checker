#!/usr/bin/env bash
# Run a docker compose command against production over SSH.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/ops/prod-compose.sh <docker compose args...>

Runs `docker compose -f docker-compose.prod.yml --env-file .env <args>` in $PROD_DIR on
$PROD_SSH (env or ssh/prod.env). Nothing is destructive unless the arguments are.

Examples:
  scripts/ops/prod-compose.sh ps
  scripts/ops/prod-compose.sh --profile observability up -d alloy
  scripts/ops/prod-compose.sh --profile observability logs --tail 50 alloy
EOF
}

if [[ $# -eq 0 || "$1" == "-h" || "$1" == "--help" ]]; then
  usage
  exit 0
fi

# shellcheck source=scripts/ops/_prod.sh
source "$(dirname "${BASH_SOURCE[0]}")/_prod.sh"
require_prod
prod_compose "$@"
