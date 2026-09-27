"""In-memory stand-in for StorageService, for tests that must not touch S3."""

from __future__ import annotations

from pathlib import Path


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def upload_bytes(
        self, data: bytes, key: str, content_type: str = "application/octet-stream"
    ) -> str:
        self.objects[key] = data
        return f"fake://{key}"

    async def upload_file(self, local_path: Path, key: str) -> str:
        self.objects[key] = local_path.read_bytes()
        return f"fake://{key}"

    async def download_bytes(self, key: str) -> bytes:
        return self.objects[key]

    async def try_download_bytes(self, key: str) -> bytes | None:
        return self.objects.get(key)

    async def object_exists(self, key: str) -> bool:
        return key in self.objects

    async def delete_file(self, key: str) -> None:
        self.objects.pop(key, None)
