#!/usr/bin/env bash
# End-to-end proof that a backup can be restored: seed → back up → destroy → restore → assert.
# Runs an isolated compose project (own volumes, no host ports); never touches the dev stack.
#
# shellcheck disable=SC2016
# Most `$VAR` references below are single-quoted on purpose: they are meant to expand
# inside the "backup" container's own shell (e.g. $RCLONE_REMOTE, set in its compose
# environment), not on this host, where they may not even be set.
set -euo pipefail
cd "$(dirname "$0")/../.."

export COMPOSE_PROJECT_NAME=subchk-backup-test
# `down -v` only removes volumes owned by services in the active profile set. backup and
# minio-init sit behind the "manual" profile (so a plain `up` never starts them) and are
# only ever invoked by name via `run`; without this, `down -v` in cleanup() silently
# leaves the "remote" volume behind on every run.
export COMPOSE_PROFILES=manual
dc() { docker compose -f docker-compose.backup-test.yml "$@"; }
psql_() { dc exec -T postgres psql -U postgres -d app -tAc "$1"; }
cleanup() { dc down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

dc build backup
dc up -d --wait postgres minio
dc run --rm minio-init

psql_ "CREATE TABLE marker(v text); INSERT INTO marker VALUES ('before');"
dc run --rm --entrypoint sh backup -c 'echo hello | rclone rcat minio:submissions-checker/roundtrip/marker.txt'

dc run --rm backup once
dc run --rm backup status

psql_ "DROP TABLE marker;"
dc run --rm --entrypoint sh backup -c 'rclone deletefile minio:submissions-checker/roundtrip/marker.txt'

dc run --rm -e RESTORE_CONFIRM=yes backup restore latest

test "$(psql_ 'SELECT v FROM marker;')" = "before"
test "$(dc run --rm --entrypoint sh backup -c 'rclone cat minio:submissions-checker/roundtrip/marker.txt')" = "hello"
dc run --rm --entrypoint sh backup -c 'test -n "$(rclone lsf "$RCLONE_REMOTE/pre-restore")"'

# Refuses to restore without confirmation.
if dc run --rm backup restore latest; then echo "restore ran without RESTORE_CONFIRM"; exit 1; fi

# Retention: seed old-stamped entries plus a recent-stamped one with an old file mtime,
# then confirm a successful run prunes by NAME stamp, not by mtime. The stamp is printed
# as the last line of stdout so the host can capture it (a fresh --rm container per line
# has no shared /tmp to stash it in).
recent_stamp="$(dc run --rm --entrypoint sh backup -c '
	set -eu
	echo old | rclone rcat "$RCLONE_REMOTE/postgres/20200101T000000Z.dump"
	echo old | rclone rcat "$RCLONE_REMOTE/minio/deleted/20200101T000000Z/x"
	recent="$(date -u +%Y%m%dT%H%M%SZ)"
	mkdir -p /tmp/retention
	printf recent > "/tmp/retention/${recent}.dump"
	touch -d 2020-01-01 "/tmp/retention/${recent}.dump"
	rclone copyto "/tmp/retention/${recent}.dump" "$RCLONE_REMOTE/postgres/${recent}.dump"
	printf "%s" "$recent"
' | tr -d '\r')"

dc run --rm backup once

dc run --rm --entrypoint sh backup -c '! rclone lsf "$RCLONE_REMOTE/postgres/20200101T000000Z.dump" 2>/dev/null | grep -q .'
dc run --rm --entrypoint sh backup -c '! rclone lsf "$RCLONE_REMOTE/minio/deleted/20200101T000000Z/" 2>/dev/null | grep -q .'
dc run --rm --entrypoint sh -e RECENT_STAMP="${recent_stamp}" backup -c 'rclone lsf "$RCLONE_REMOTE/postgres/${RECENT_STAMP}.dump" | grep -q .'

# A failed run neither prunes nor writes last-success: break postgres and compare.
before="$(dc run --rm --entrypoint sh backup -c 'rclone cat "$RCLONE_REMOTE/last-success"')"
dc stop postgres
if dc run --rm backup once; then echo "backup succeeded with postgres down"; exit 1; fi
after="$(dc run --rm --entrypoint sh backup -c 'rclone cat "$RCLONE_REMOTE/last-success"')"
test "$before" = "$after"

echo "backup roundtrip OK"
