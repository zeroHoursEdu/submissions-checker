# Backup sidecar: pg_dump for the database, mc for the object store.
#
# mc is copied from MinIO's own image rather than installed from Alpine, which ships it
# renamed to `mcli` to avoid colliding with midnight commander. Copying the official
# static binary keeps the name and the version predictable.
FROM quay.io/minio/mc:latest AS mc

FROM alpine:3.20

# postgresql-client must match the server's major version (16) — a newer pg_dump can
# read an older server, but an older pg_dump refuses a newer one.
RUN apk add --no-cache postgresql16-client tzdata

COPY --from=mc /usr/bin/mc /usr/local/bin/mc

COPY docker/backup/backup.sh /usr/local/bin/backup.sh
RUN chmod +x /usr/local/bin/backup.sh /usr/local/bin/mc

ENTRYPOINT ["/usr/local/bin/backup.sh"]
