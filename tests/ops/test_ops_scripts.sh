#!/usr/bin/env bash
# Offline checks for scripts/ops: help text, config errors, and the exact remote commands.
set -euo pipefail
cd "$(dirname "$0")/../.."
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/bin"
cat > "$tmp/bin/ssh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$SSH_LOG"
# FAKE_SSH_FAIL_ON lets a test simulate a remote command failing (e.g. `stop app`),
# to check what runs after it. Stdin is always drained so a caller piping data (SQL on
# stdin, or a closed stdin) never blocks or gets SIGPIPE, and captured to $SSH_STDIN
# when a test wants to inspect exactly what would have reached the remote command.
if [[ -n "${FAKE_SSH_FAIL_ON:-}" && "$*" == *"${FAKE_SSH_FAIL_ON}"* ]]; then
  cat >/dev/null
  exit 1
fi
if [[ -n "${SSH_STDIN:-}" ]]; then
  cat > "$SSH_STDIN"
else
  cat >/dev/null
fi
EOF
chmod +x "$tmp/bin/ssh"
export PATH="$tmp/bin:$PATH" SSH_LOG="$tmp/ssh.log" PROD_ENV_FILE="$tmp/none"

declare -A valid_args=(
  [connect-to-prod-db]='-c select-1'
  [run-prod-backup]='--status'
  [restore-prod-from-backup]='latest --yes'
)
for s in "${!valid_args[@]}"; do
  # shellcheck disable=SC2086
  scripts/ops/$s.sh --help | grep -q "Usage:" || { echo "$s: no usage"; exit 1; }
  # SC1007: `VAR=` with a following space is deliberate here (unset for the next command only).
  # shellcheck disable=SC2086,SC1007
  if PROD_SSH= PROD_DIR= scripts/ops/$s.sh ${valid_args[$s]} 2>"$tmp/err"; then echo "$s ran without PROD_SSH"; exit 1; fi
  grep -q "PROD_SSH" "$tmp/err" || { echo "$s: unclear error"; exit 1; }
done
test ! -s "$SSH_LOG" || { echo "a script reached ssh without config"; exit 1; }

export PROD_SSH=user@host PROD_DIR=/srv/app
scripts/ops/run-prod-backup.sh --status
grep -q "exec -T backup backup.sh status" "$SSH_LOG"

scripts/ops/connect-to-prod-db.sh -c "select 1"
grep -q "default_transaction_read_only=on" "$SSH_LOG"

# A multi-line SQL string with quotes, a `$`, a tab and a newline must reach psql
# byte-identical, and none of it may appear on the remote command line: a `printf %q`
# of it (needed twice over, once for the SQL and once for the whole argument list
# crossing the ssh boundary) is not portable across the two remote shell layers — for
# control characters bash's %q emits `$'...'`, which the container's POSIX sh cannot
# parse, so a multi-line statement would get word-split into something else entirely.
# SQL travels on stdin instead; the command line stays fully static.
: > "$SSH_LOG"
stdin_capture="$tmp/stdin.log"
tricky_sql=$'select \'a\tb\' as "x", \'$1\' -- comment\nline2 with a\ttab'
SSH_STDIN="$stdin_capture" scripts/ops/connect-to-prod-db.sh -c "$tricky_sql"
diff <(printf '%s\n' "$tricky_sql") "$stdin_capture" >/dev/null \
  || { echo "connect-to-prod-db: SQL did not arrive on stdin byte-identical"; exit 1; }
grep -qF "line2 with a" "$SSH_LOG" && { echo "connect-to-prod-db: SQL text leaked into the remote command line"; exit 1; }
grep -q "postgres sh -c" "$SSH_LOG" || { echo "connect-to-prod-db: psql was not invoked"; exit 1; }

: > "$SSH_LOG"
if echo "no" | scripts/ops/restore-prod-from-backup.sh latest; then echo "restore ran without confirmation"; exit 1; fi
test ! -s "$SSH_LOG" || { echo "restore touched the host without confirmation"; exit 1; }

# A closed stdin makes `read` itself fail (no line to read); the script must say so
# distinctly instead of exiting silently via `set -e`, and still must not touch the host.
: > "$SSH_LOG"
if scripts/ops/restore-prod-from-backup.sh latest < /dev/null 2>"$tmp/err"; then
  echo "restore ran despite closed stdin"; exit 1
fi
grep -q "aborted (no confirmation)" "$tmp/err" || { echo "restore: missing no-confirmation message"; exit 1; }
test ! -s "$SSH_LOG" || { echo "restore touched the host despite closed stdin"; exit 1; }

# The EXIT trap that restarts `app` must be armed before `stop app` runs, not after —
# otherwise a `stop app` that itself fails under `set -e` leaves the app down for good.
: > "$SSH_LOG"
if FAKE_SSH_FAIL_ON='stop app' scripts/ops/restore-prod-from-backup.sh latest --yes; then
  echo "restore did not propagate the stop-app failure"; exit 1
fi
grep -q "up -d app" "$SSH_LOG" || { echo "restore-prod-from-backup: app was not restarted after stop failed"; exit 1; }
grep -q "RESTORE_CONFIRM=yes backup restore latest" "$SSH_LOG" && { echo "restore proceeded despite stop app failing"; exit 1; }

: > "$SSH_LOG"
scripts/ops/restore-prod-from-backup.sh latest --yes
grep -q "stop app" "$SSH_LOG"
grep -q "RESTORE_CONFIRM=yes backup restore latest" "$SSH_LOG"
grep -q "up -d app" "$SSH_LOG"
# prod-compose.sh: an interactive `run`/`exec` (the one-time `claude` /login) needs a remote
# TTY; non-interactive subcommands and `-T` must not get one.
: > "$SSH_LOG"
scripts/ops/prod-compose.sh --profile llm run --rm -it llm-judge claude
grep -q "^-t " "$SSH_LOG" || { echo "prod-compose run: no ssh -t"; exit 1; }
: > "$SSH_LOG"
scripts/ops/prod-compose.sh exec llm-judge sh
grep -q "^-t " "$SSH_LOG" || { echo "prod-compose exec: no ssh -t"; exit 1; }
: > "$SSH_LOG"
scripts/ops/prod-compose.sh --profile llm exec -T llm-judge curl -fsS http://127.0.0.1:8090/health
grep -q "^-t " "$SSH_LOG" && { echo "prod-compose exec -T got a tty"; exit 1; }
: > "$SSH_LOG"
scripts/ops/prod-compose.sh ps
grep -q "^-t " "$SSH_LOG" && { echo "prod-compose ps got a tty"; exit 1; }
grep -q "ps" "$SSH_LOG"
scripts/ops/prod-compose.sh --help | grep -q -- "-t" || { echo "prod-compose --help omits TTY note"; exit 1; }
echo "ops scripts OK"
