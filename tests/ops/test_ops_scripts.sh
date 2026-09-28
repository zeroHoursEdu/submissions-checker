#!/usr/bin/env bash
# Offline checks for scripts/ops: help text, config errors, and the exact remote commands.
set -euo pipefail
cd "$(dirname "$0")/../.."
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/bin"
cat > "$tmp/bin/ssh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$SSH_LOG"
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

# A SQL string with quotes, spaces and a `$` must reach the remote intact. _prod.sh
# quotes with `printf %q` twice over (once for the SQL, once for the whole argument
# list crossing the ssh boundary), so decode both layers the same way a real remote
# shell would (word-split + quote-removal via `eval "set -- ..."`) and compare.
decode_qwords() {
  # The inner script references $POSTGRES_USER/$POSTGRES_DB the way the real remote
  # shell would expand them; this test only cares about the trailing SQL token, so
  # tolerate them being unset instead of tripping our own `set -u`.
  set +u
  eval "set -- $1"
  set -u
  printf '%s' "${!#}"
}

: > "$SSH_LOG"
tricky_sql="select 'a b' as \"x\", '\$1'"
scripts/ops/connect-to-prod-db.sh -c "$tricky_sql"
logged="$(cat "$SSH_LOG")"
outer_tokens="${logged#*--env-file .env }"
inner_script="$(decode_qwords "$outer_tokens")"
decoded_sql="$(decode_qwords "$inner_script")"
[[ "$decoded_sql" == "$tricky_sql" ]] || { echo "connect-to-prod-db: SQL was not safely quoted (got: $decoded_sql)"; exit 1; }

: > "$SSH_LOG"
if echo "no" | scripts/ops/restore-prod-from-backup.sh latest; then echo "restore ran without confirmation"; exit 1; fi
test ! -s "$SSH_LOG" || { echo "restore touched the host without confirmation"; exit 1; }

scripts/ops/restore-prod-from-backup.sh latest --yes
grep -q "stop app" "$SSH_LOG"
grep -q "RESTORE_CONFIRM=yes backup restore latest" "$SSH_LOG"
grep -q "up -d app" "$SSH_LOG"
echo "ops scripts OK"
