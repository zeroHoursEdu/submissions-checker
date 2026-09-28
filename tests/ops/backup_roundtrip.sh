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
# leaves the "remote"/"lock" volumes behind on every run.
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

# I1: the lock is a shared named volume, not container-local /tmp — a `run --rm` must be
# refused while another instance (standing in for the long-lived `schedule` replica, which
# shares the same volume) already holds it.
dc run --rm --name subchk-backup-test-lock-holder --entrypoint sh backup \
	-c 'exec 9>/var/lock/backup/lock; flock 9; sleep 30' >/dev/null 2>&1 &
lock_holder_pid=$!
# Give the holder time to start and actually acquire the flock before racing it.
sleep 3
if lock_out="$(dc run --rm backup once 2>&1)"; then
	echo "backup once ran while another instance holds the lock"
	exit 1
fi
echo "${lock_out}" | grep -q "another backup or restore is running" \
	|| { echo "expected lock-contention message, got: ${lock_out}"; exit 1; }
# -t 1: its PID 1 (plain `sh`) doesn't react to SIGTERM, so the default 10s grace period
# would just be wasted time here.
docker stop -t 1 subchk-backup-test-lock-holder >/dev/null 2>&1 || true
wait "${lock_holder_pid}" 2>/dev/null || true

# I3: restore "latest" must resolve via last-success, not "the newest dump by name" — a
# dump can exist for a run that later failed (here, one that was never a real dump at
# all). If `restore latest` picked this by name, pg_restore would fail on garbage bytes.
dc run --rm --entrypoint sh backup -c 'echo not-a-real-dump | rclone rcat "$RCLONE_REMOTE/postgres/29990101T000000Z.dump"'

# F8: `list` must flag that same dump as newer than last-success — it exists only because
# a run got as far as uploading a dump and never became a real backup — rather than
# presenting it as an ordinary restore point indistinguishable from a good one.
list_out="$(dc run --rm backup list)"
echo "${list_out}"
echo "${list_out}" | grep -q "^29990101T000000Z  (after last successful backup — objects may be incomplete)$" \
	|| { echo "list did not flag a dump newer than last-success"; exit 1; }

dc run --rm -e RESTORE_CONFIRM=yes backup restore latest
test "$(psql_ 'SELECT v FROM marker;')" = "before"
dc run --rm --entrypoint sh backup -c 'rclone deletefile "$RCLONE_REMOTE/postgres/29990101T000000Z.dump"'

# I2: restore's bucket sync must not permanently delete an object uploaded to the live
# bucket since the last backup — it should end up moved to pre-restore/<safety>/, right
# next to the safety pg_dump of the same stamp, not simply gone.
dc run --rm --entrypoint sh backup -c 'echo new | rclone rcat minio:submissions-checker/roundtrip/after-backup.txt'
restore_log="$(dc run --rm -e RESTORE_CONFIRM=yes backup restore latest 2>&1)"
echo "${restore_log}"
safety_stamp="$(printf '%s\n' "${restore_log}" | sed -n 's#.*pre-restore/\([0-9]\{8\}T[0-9]\{6\}Z\)\.dump.*#\1#p' | head -n1)"
[ -n "${safety_stamp}" ] || { echo "could not find a safety stamp in the restore output"; exit 1; }
dc run --rm --entrypoint sh backup -c 'rclone lsf minio:submissions-checker/roundtrip/after-backup.txt' | grep -q . \
	&& { echo "object survived in the live bucket after restore (should have moved to pre-restore/)"; exit 1; }
test "$(dc run --rm --entrypoint sh -e SAFETY="${safety_stamp}" backup \
	-c 'rclone cat "$RCLONE_REMOTE/pre-restore/$SAFETY/roundtrip/after-backup.txt"')" = "new"

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

# I4: restore must work on a fresh host where the bucket does not exist yet — preflight
# must create it (mkdir), not merely try to read it and die when there is nothing there.
dc run --rm --entrypoint sh backup -c 'rclone purge minio:submissions-checker'
dc run --rm -e RESTORE_CONFIRM=yes backup restore latest
test "$(dc run --rm --entrypoint sh backup -c 'rclone cat minio:submissions-checker/roundtrip/marker.txt')" = "hello"

# A failed run neither prunes nor writes last-success: break postgres and compare.
before="$(dc run --rm --entrypoint sh backup -c 'rclone cat "$RCLONE_REMOTE/last-success"')"
dc stop postgres
if dc run --rm backup once; then echo "backup succeeded with postgres down"; exit 1; fi
after="$(dc run --rm --entrypoint sh backup -c 'rclone cat "$RCLONE_REMOTE/last-success"')"
test "${before}" = "${after}"

echo "backup roundtrip OK"
