#!/bin/sh
# Scheduled backup of both stores that hold data we cannot rebuild:
# the PostgreSQL database and the MinIO bucket (proctoring evidence, assignment files).
#
# Restoring only one of the two leaves dangling references — snapshot rows pointing at
# objects that no longer exist — so both are captured in the same run.
set -eu

BACKUP_ROOT=/backups
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
INTERVAL="${BACKUP_INTERVAL_SECONDS:-86400}"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

run_once() {
	stamp="$(date -u +%Y%m%dT%H%M%SZ)"
	failed=0

	log "backup ${stamp} starting"

	mkdir -p "${BACKUP_ROOT}/postgres" "${BACKUP_ROOT}/minio"

	# --- database -------------------------------------------------------------
	# Custom format: compressed, and restorable selectively with pg_restore.
	if PGPASSWORD="${POSTGRES_PASSWORD}" pg_dump \
		--host=postgres \
		--username="${POSTGRES_USER}" \
		--dbname="${POSTGRES_DB}" \
		--format=custom \
		--file="${BACKUP_ROOT}/postgres/${stamp}.dump"; then
		log "database dump written: postgres/${stamp}.dump"
	else
		log "ERROR: database dump failed"
		rm -f "${BACKUP_ROOT}/postgres/${stamp}.dump"
		failed=1
	fi

	# --- object storage -------------------------------------------------------
	if mc alias set backup "http://minio:9000" "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}" >/dev/null 2>&1 &&
		mc mirror --overwrite --remove "backup/${S3_BUCKET_NAME}" "${BACKUP_ROOT}/minio/${S3_BUCKET_NAME}"; then
		log "object storage mirrored: minio/${S3_BUCKET_NAME}"
	else
		log "ERROR: object storage mirror failed"
		failed=1
	fi

	# --- retention ------------------------------------------------------------
	# Prune only on a successful run: a failing backup must not also delete the last
	# good one, which would turn one bad night into total data loss.
	if [ "${failed}" -eq 0 ]; then
		find "${BACKUP_ROOT}/postgres" -name '*.dump' -mtime "+${RETENTION_DAYS}" -print -delete
		log "pruned dumps older than ${RETENTION_DAYS} days"
	else
		log "skipping prune because this run failed"
	fi

	if [ "${failed}" -ne 0 ]; then
		log "backup ${stamp} FAILED"
		return 1
	fi
	log "backup ${stamp} complete"
	return 0
}

if [ "${1:-loop}" = "once" ]; then
	run_once
	exit $?
fi

while true; do
	run_once || log "continuing despite failure; next attempt in ${INTERVAL}s"
	sleep "${INTERVAL}"
done
