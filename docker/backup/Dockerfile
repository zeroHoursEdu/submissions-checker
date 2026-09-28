# Backup sidecar: pg_dump for the database, rclone for the object store and the off-host
# copy (Google Drive in production; any rclone remote or a plain path works).
FROM alpine:3.20

# postgresql-client must match the server's major version (16) — a newer pg_dump can
# read an older server, but an older pg_dump refuses a newer one.
RUN apk add --no-cache postgresql16-client rclone tzdata

COPY docker/backup/backup.sh /usr/local/bin/backup.sh
RUN chmod +x /usr/local/bin/backup.sh

ENTRYPOINT ["/usr/local/bin/backup.sh"]
CMD ["schedule"]
