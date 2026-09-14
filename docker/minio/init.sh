#!/bin/sh
# Create the application bucket on first start. Runs to completion and exits.
#
# The bucket is deliberately left PRIVATE. Proctoring evidence is served only through
# the application's authenticated endpoint; making this bucket public would put students'
# webcam frames on the internet for anyone holding the object key.
set -eu

until mc alias set local "http://minio:9000" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" 2>/dev/null; do
	echo "waiting for minio..."
	sleep 2
done

mc mb --ignore-existing "local/${S3_BUCKET_NAME}"
mc anonymous set none "local/${S3_BUCKET_NAME}"

echo "bucket ${S3_BUCKET_NAME} ready (private)"
