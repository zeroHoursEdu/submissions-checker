#!/bin/sh
# Backups of the two stores that hold data we cannot rebuild — PostgreSQL and the MinIO
# bucket — to an rclone remote ($RCLONE_REMOTE, e.g. gdrive:subchk). Nothing is kept on
# the local disk: a backup next to the database does not survive losing that disk.
#
#   backup.sh schedule        preflight, then run `once` on $BACKUP_CRON (the default)
#   backup.sh once            one backup now
#   backup.sh status          last successful backup and its age; non-zero if too old
#   backup.sh list            available database dumps, oldest first
#   backup.sh restore STAMP   restore DB + bucket from STAMP or `latest` (RESTORE_CONFIRM=yes)
#
# Remote layout:
#   postgres/<stamp>.dump          pg_dump --format=custom
#   minio/current/                 mirror of the bucket
#   minio/deleted/<stamp>/         objects that run removed or overwrote in the mirror
#   pre-restore/<stamp>.dump       the database as it was just before a restore
#   pre-restore/<stamp>/           the whole bucket as it was just before that restore
#   last-success                   stamp of the last fully successful run
set -eu

REMOTE="${RCLONE_REMOTE:-}"
BUCKET="minio:${S3_BUCKET_NAME:-submissions-checker}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
MAX_AGE_HOURS="${BACKUP_MAX_AGE_HOURS:-12}"
# A named volume, not container-local /tmp: `docker compose run --rm backup once|restore`
# spins up a brand-new container, so a /tmp lock only ever serializes a run against
# itself, never against the long-lived `schedule` replica's own cron-triggered runs.
LOCK=/var/lock/backup/lock
ENV_FILE=/tmp/backup.env
# Keep rclone's memory inside the container limit on a 2GB host.
# shellcheck disable=SC2034  # used unquoted on purpose wherever rclone takes flags
RCLONE_FLAGS="--transfers 2 --checkers 4 --buffer-size 8M"

# Temp files this run creates, removed on every exit path — success, die(), or a signal —
# rather than an inline `rm -f` after each risky step, so a die() several steps after a
# file is created still cleans it up.
TMP_FILES=""
track_tmp() { TMP_FILES="${TMP_FILES} $1"; }
# shellcheck disable=SC2086  # word-splitting the tracked paths is the point
cleanup_tmp() { rm -f ${TMP_FILES}; }
trap cleanup_tmp EXIT

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }
die() { log "ERROR: $*"; exit 1; }
stamp_now() { date -u +%Y%m%dT%H%M%SZ; }
pg() { PGPASSWORD="${POSTGRES_PASSWORD}" "$@" --host=postgres --username="${POSTGRES_USER}" --dbname="${POSTGRES_DB}"; }

# 20260928T060000Z -> epoch seconds (busybox date understands "YYYY-MM-DD hh:mm:ss").
stamp_epoch() {
	s="$1"
	date -u -d "$(echo "$s" | sed -E 's/^(....)(..)(..)T(..)(..)(..)Z$/\1-\2-\3 \4:\5:\6/')" +%s
}

preflight() {
	[ -n "${REMOTE}" ] || die "RCLONE_REMOTE is not set. Backups are off-host only; see docs/deployment.md#backups-and-restore"
	# A remote with no "name:" prefix is a plain filesystem path to rclone — i.e. this
	# container's (or, worse, the prod host's) own disk. That defeats the entire point
	# of an off-host backup, so refuse it unless a test opts in explicitly.
	case "${REMOTE}" in
		*:*) ;;
		*)
			[ "${BACKUP_ALLOW_LOCAL_REMOTE:-}" = "1" ] \
				|| die "RCLONE_REMOTE '${REMOTE}' has no 'name:' prefix — refusing to write backups onto local disk (set BACKUP_ALLOW_LOCAL_REMOTE=1 for tests only)"
			;;
	esac
	rclone mkdir "${REMOTE}" || die "cannot reach ${REMOTE} — check rclone.conf (RCLONE_CONFIG=${RCLONE_CONFIG:-default})"
	# mkdir, not lsf: it is idempotent when the bucket already exists, and unlike a read
	# it also succeeds when restoring onto a fresh host whose bucket does not exist yet.
	rclone mkdir "${BUCKET}" || die "cannot reach ${BUCKET} — check MinIO credentials"
	pg pg_isready >/dev/null || die "postgres is not ready"
}

# Delete dated entries (files "<stamp>.dump" or dirs "<stamp>/") older than the retention.
# Decided by the stamp in the NAME: rclone --backup-dir keeps an object's original mtime,
# so an mtime rule would delete a just-removed old object on the very next run.
prune_dir() {
	dir="$1"
	cutoff=$(( $(date -u +%s) - RETENTION_DAYS * 86400 ))
	rclone lsf "${dir}" 2>/dev/null | while IFS= read -r entry; do
		s="${entry%%.dump}"; s="${s%/}"
		case "$s" in [0-9]*T*Z) ;; *) continue ;; esac
		if [ "$(stamp_epoch "$s")" -lt "${cutoff}" ]; then
			case "${entry}" in
				*/) rclone purge "${dir}/${s}" ;;
				*) rclone deletefile "${dir}/${entry}" ;;
			esac
			log "pruned ${dir}/${entry}"
		fi
	done
}

run_once() {
	# A crash mid-run can leave a partial dump in /tmp; the shared lock guarantees no
	# other run is using one right now, so it is always safe to clear before starting.
	rm -f /tmp/*.dump
	preflight
	stamp="$(stamp_now)"
	dump="/tmp/${stamp}.dump"
	track_tmp "${dump}"
	log "backup ${stamp} starting"

	# Database first, objects second: every row in the dump then has its object in the
	# mirror. The reverse order could capture rows whose objects were uploaded later.
	pg pg_dump --format=custom --file="${dump}" || die "pg_dump failed"
	# shellcheck disable=SC2086  # RCLONE_FLAGS is a deliberately unquoted word list
	rclone copyto ${RCLONE_FLAGS} "${dump}" "${REMOTE}/postgres/${stamp}.dump" || die "dump upload failed"
	rm -f "${dump}"
	log "database: postgres/${stamp}.dump"

	# shellcheck disable=SC2086  # RCLONE_FLAGS is a deliberately unquoted word list
	rclone sync ${RCLONE_FLAGS} "${BUCKET}" "${REMOTE}/minio/current" \
		--backup-dir "${REMOTE}/minio/deleted/${stamp}" || die "bucket sync failed"
	log "objects: minio/current (changes kept in minio/deleted/${stamp})"

	# Only a fully successful run prunes: a failing backup must never also delete the
	# last good one.
	prune_dir "${REMOTE}/postgres"
	prune_dir "${REMOTE}/minio/deleted"
	prune_dir "${REMOTE}/pre-restore"
	echo "${stamp}" | rclone rcat "${REMOTE}/last-success"
	log "backup ${stamp} complete"
}

locked() {
	exec 9>"${LOCK}"
	flock -n 9 || die "another backup or restore is running"
	"$@"
}

status() {
	last="$(rclone cat "${REMOTE}/last-success" 2>/dev/null)" || { echo "no successful backup yet"; exit 1; }
	age_h=$(( ($(date -u +%s) - $(stamp_epoch "${last}")) / 3600 ))
	echo "last successful backup: ${last} (${age_h}h ago)"
	[ "${age_h}" -le "${MAX_AGE_HOURS}" ] || { echo "OLDER THAN ${MAX_AGE_HOURS}h"; exit 1; }
}

# Only names shaped like a backup stamp — a stray file dropped in postgres/ (or a dump
# from a run that failed later, e.g. at the bucket sync) should not show up as a choice.
list() {
	rclone lsf "${REMOTE}/postgres" 2>/dev/null | while IFS= read -r entry; do
		s="${entry%.dump}"
		case "$s" in [0-9]*T*Z) echo "$s" ;; esac
	done | sort
}

restore() {
	[ "${RESTORE_CONFIRM:-}" = "yes" ] || die "restore overwrites the database and bucket; set RESTORE_CONFIRM=yes"
	preflight
	want="${1:-}"
	[ -n "${want}" ] || die "usage: backup.sh restore <stamp|latest>"
	if [ "${want}" = "latest" ]; then
		# From last-success, not "the newest dump": a dump can exist for a run that
		# then failed at the bucket sync, which is not a backup anyone should restore.
		want="$(rclone cat "${REMOTE}/last-success" 2>/dev/null)" \
			|| die "no successful backup recorded at ${REMOTE}/last-success"
	fi
	[ -n "${want}" ] || die "no dumps found in ${REMOTE}/postgres"
	rclone lsf "${REMOTE}/postgres/${want}.dump" | grep -q . || die "no dump ${want}"

	safety="$(stamp_now)"
	safety_dump="/tmp/pre-${safety}.dump"
	track_tmp "${safety_dump}"
	pg pg_dump --format=custom --file="${safety_dump}" || die "safety dump failed; nothing changed"
	rclone copyto "${safety_dump}" "${REMOTE}/pre-restore/${safety}.dump" || die "safety dump upload failed; nothing changed"
	rm -f "${safety_dump}"
	log "current database saved to pre-restore/${safety}.dump"

	restore_dump="/tmp/${want}.dump"
	track_tmp "${restore_dump}"
	rclone copyto "${REMOTE}/postgres/${want}.dump" "${restore_dump}" \
		|| die "download of postgres/${want}.dump failed; database unchanged"
	# One transaction: a failed restore leaves the database exactly as it was.
	pg pg_restore --clean --if-exists --no-owner --single-transaction "${restore_dump}" \
		|| die "pg_restore failed; database unchanged (transaction rolled back)"
	rm -f "${restore_dump}"
	log "database restored from postgres/${want}.dump"

	# An object uploaded to the bucket after the last backup would otherwise be deleted
	# permanently by the sync below instead of merely restored to an older state.
	# rclone's own --backup-dir can't do this here: it requires the backup directory to
	# be on the SAME remote as the sync destination, and the destination is the "minio:"
	# S3 remote while $REMOTE is the off-host store (confirmed: "parameter to
	# --backup-dir has to be on the same remote as destination"). A plain `copy` has no
	# such restriction, so take a full snapshot of the live bucket to the same path a
	# --backup-dir would have used — pre-restore/<safety>/, next to pre-restore/
	# <safety>.dump — before overwriting it; the existing name-based prune already
	# covers <stamp>/ dirs there.
	# shellcheck disable=SC2086  # RCLONE_FLAGS is a deliberately unquoted word list
	rclone copy ${RCLONE_FLAGS} "${BUCKET}" "${REMOTE}/pre-restore/${safety}" \
		|| die "could not snapshot the live bucket to pre-restore/${safety}/; nothing changed"
	# shellcheck disable=SC2086  # RCLONE_FLAGS is a deliberately unquoted word list
	rclone sync ${RCLONE_FLAGS} "${REMOTE}/minio/current" "${BUCKET}" \
		|| die "bucket restore failed (database already restored)"
	log "bucket restored from minio/current (previous bucket contents saved to pre-restore/${safety}/)"
}

schedule() {
	preflight
	cron="${BACKUP_CRON:-0 */6 * * *}"
	# busybox crond does not pass the container environment to jobs. The file holds
	# secrets (POSTGRES_PASSWORD, rclone keys) — keep it owner-only.
	umask 077
	export -p > "${ENV_FILE}"
	echo "${cron} . ${ENV_FILE}; /usr/local/bin/backup.sh once > /proc/1/fd/1 2>&1" > /etc/crontabs/root
	log "scheduled '${cron}' (TZ=${TZ:-UTC}) to ${REMOTE}"
	exec crond -f -l 8
}

cmd="${1:-schedule}"
[ $# -gt 0 ] && shift
case "${cmd}" in
	schedule) schedule ;;
	once) locked run_once ;;
	status) status ;;
	list) list ;;
	restore) locked restore "$@" ;;
	*) die "unknown command: ${cmd} (schedule|once|status|list|restore)" ;;
esac
