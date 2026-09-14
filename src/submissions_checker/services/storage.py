"""S3-compatible object storage service for subject images and assignment content files."""

from __future__ import annotations

from pathlib import Path

import aioboto3

from submissions_checker.core.config import Settings
from submissions_checker.core.logging import get_logger

logger = get_logger(__name__)


class StorageService:
    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.s3_bucket_name
        self._public_base_url = settings.s3_public_base_url
        self._session = aioboto3.Session(
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            region_name=settings.aws_region,
        )
        self._endpoint_url = settings.s3_endpoint_url

    async def upload_file(self, local_path: Path, key: str) -> str:
        """Upload a file to S3 and return the URL used to address it.

        Objects are written without a public ACL: the bucket is not internet-reachable
        in production, and possession of a key must not be enough to read an object.
        Anything user-visible is served through an authenticated application route.
        """
        async with self._session.client("s3", endpoint_url=self._endpoint_url) as s3:
            with local_path.open("rb") as f:
                await s3.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=f,
                )
        url = self._build_url(key)
        logger.info("file_uploaded", key=key, url=url)
        return url

    async def upload_bytes(
        self, data: bytes, key: str, content_type: str = "application/octet-stream"
    ) -> str:
        """Upload an in-memory byte payload to S3 and return the URL used to address it.

        Written without a public ACL — see ``upload_file``.
        """
        async with self._session.client("s3", endpoint_url=self._endpoint_url) as s3:
            await s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
        url = self._build_url(key)
        logger.info("bytes_uploaded", key=key, url=url, size=len(data))
        return url

    async def download_bytes(self, key: str) -> bytes:
        """Read an object back from S3.

        Used by the authenticated routes that serve private objects (proctoring
        evidence) so the client never addresses object storage directly.
        """
        async with self._session.client("s3", endpoint_url=self._endpoint_url) as s3:
            response = await s3.get_object(Bucket=self._bucket, Key=key)
            # aioboto3's streaming body is untyped, so the read result is Any.
            data: bytes = await response["Body"].read()
            return data

    async def delete_file(self, key: str) -> None:
        """Delete an object from S3."""
        async with self._session.client("s3", endpoint_url=self._endpoint_url) as s3:
            await s3.delete_object(Bucket=self._bucket, Key=key)
        logger.info("file_deleted", key=key)

    def _build_url(self, key: str) -> str:
        if self._public_base_url:
            return f"{self._public_base_url.rstrip('/')}/{key}"
        if self._endpoint_url:
            return f"{self._endpoint_url.rstrip('/')}/{self._bucket}/{key}"
        return f"https://{self._bucket}.s3.amazonaws.com/{key}"
