# shellcheck shell=bash
# Shared helpers for scripts/ops. Sourced; do not run directly.
# Production address comes from the environment or ssh/prod.env — never from git.
PROD_ENV_FILE="${PROD_ENV_FILE:-$(dirname "${BASH_SOURCE[0]}")/../../ssh/prod.env}"
# shellcheck disable=SC1090
[[ -f "$PROD_ENV_FILE" ]] && source "$PROD_ENV_FILE"

require_prod() {
  if [[ -z "${PROD_SSH:-}" || -z "${PROD_DIR:-}" ]]; then
    echo "PROD_SSH and PROD_DIR must be set (env or ssh/prod.env; see scripts/ops/prod.env.example)" >&2
    exit 2
  fi
}

SSH_TTY_FLAG=()

# Run a compose command on the production host: prod_compose exec -T backup backup.sh status
prod_compose() {
  # SC2029: the client-side expansion of $PROD_DIR and "$@" is the point — printf %q
  # quotes them so the remote shell reconstructs the exact same argument list.
  # ${SSH_TTY_FLAG[@]+"${SSH_TTY_FLAG[@]}"}, not "${SSH_TTY_FLAG[@]}": under `set -u`,
  # expanding an empty array with the plain form is an error on bash < 4.4 (e.g. the
  # /bin/bash 3.2 that ships with macOS).
  # shellcheck disable=SC2029
  ssh ${SSH_TTY_FLAG[@]+"${SSH_TTY_FLAG[@]}"} "$PROD_SSH" \
    "cd $(printf %q "$PROD_DIR") && docker compose -f docker-compose.prod.yml --env-file .env $(printf '%q ' "$@")"
}
