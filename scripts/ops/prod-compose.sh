#!/usr/bin/env bash
# Run a docker compose command against production over SSH.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/ops/prod-compose.sh <docker compose args...>

Runs `docker compose -f docker-compose.prod.yml --env-file .env <args>` in $PROD_DIR on
$PROD_SSH (env or ssh/prod.env). Nothing is destructive unless the arguments are.
`run` and `exec` get a remote TTY (ssh -t) so interactive use works (e.g. the one-time
`run --rm -it llm-judge claude` login); pass -T to either to run without one.

Examples:
  scripts/ops/prod-compose.sh ps
  scripts/ops/prod-compose.sh --profile observability up -d alloy
  scripts/ops/prod-compose.sh --profile observability logs --tail 50 alloy
  scripts/ops/prod-compose.sh --profile llm run --rm -it llm-judge claude
EOF
}

if [[ $# -eq 0 || "$1" == "-h" || "$1" == "--help" ]]; then
  usage
  exit 0
fi

# shellcheck source=scripts/ops/_prod.sh
source "$(dirname "${BASH_SOURCE[0]}")/_prod.sh"
require_prod
# Interactive run/exec need a remote TTY; -T asks compose for none, so ssh shouldn't either.
want_tty=0 no_tty=0
for a in "$@"; do
  case "$a" in
    run | exec) want_tty=1 ;;
    -T) no_tty=1 ;;
  esac
done
if [[ $want_tty -eq 1 && $no_tty -eq 0 ]]; then
  SSH_TTY_FLAG=(-t)
fi
prod_compose "$@"
