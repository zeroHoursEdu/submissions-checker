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
        """Upload a file to S3 and return its public URL."""
        async with self._session.client("s3", endpoint_url=self._endpoint_url) as s3:
            with local_path.open("rb") as f:
                await s3.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=f,
                    ACL="public-read",
                )
        url = self._build_url(key)
        logger.info("file_uploaded", key=key, url=url)
        return url

    def _build_url(self, key: str) -> str:
        if self._public_base_url:
            return f"{self._public_base_url.rstrip('/')}/{key}"
        if self._endpoint_url:
            return f"{self._endpoint_url.rstrip('/')}/{self._bucket}/{key}"
        return f"https://{self._bucket}.s3.amazonaws.com/{key}"
