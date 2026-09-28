"""One-time backfill: copy legacy local submission ZIPs into object storage.

    docker compose -f docker-compose.prod.yml --env-file .env run --rm app \
        python -m submissions_checker.cli.migrate_uploads

Idempotent — objects already present are skipped, so it is safe to re-run. Also lists
subjects whose latest config predates stored archives (migration 0018): those must be
re-applied before the legacy plugins directory can be removed.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core.config import get_settings
from submissions_checker.db.models import SubjectPluginConfig, Submission
from submissions_checker.db.models.enums import SubmissionSourceType
from submissions_checker.db.session import get_session
from submissions_checker.services.storage import StorageService, get_storage
from submissions_checker.services.submission_files import LEGACY_UPLOADS_DIR, submission_key


@dataclass
class MigrationReport:
    copied: int = 0
    present: int = 0
    missing: list[int] = field(default_factory=list)
    subjects_without_archive: list[str] = field(default_factory=list)


async def migrate_uploads(
    db: AsyncSession, storage: StorageService, uploads_dir: Path
) -> MigrationReport:
    report = MigrationReport()
    rows = await db.execute(
        select(Submission.id, Submission.source_metadata)
        .where(Submission.source_type == SubmissionSourceType.ZIP_UPLOAD)
        .order_by(Submission.id)
    )
    for sub_id, meta in rows:
        saved_as = (meta or {}).get("saved_as")
        try:
            key = submission_key(saved_as or "")
        except ValueError:
            report.missing.append(sub_id)
            continue
        if await storage.object_exists(key):
            report.present += 1
            continue
        # submission_key already rejected a falsy/unsafe name above, so saved_as is a
        # real string here; the `or ""` only reassures mypy that it isn't None.
        path = uploads_dir / (saved_as or "")
        if path.is_file():
            await storage.upload_file(path, key)
            report.copied += 1
        else:
            report.missing.append(sub_id)

    # Subject.code is set only by the startup PluginLoader path; ZIP-uploaded configs
    # only carry the code inside their JSON config, so read it from there instead.
    latest = (
        select(SubjectPluginConfig.subject_id, func.max(SubjectPluginConfig.version).label("v"))
        .group_by(SubjectPluginConfig.subject_id)
        .subquery()
    )
    codes = await db.scalars(
        select(SubjectPluginConfig.config["subjectCode"].astext)
        .join(
            latest,
            (latest.c.subject_id == SubjectPluginConfig.subject_id)
            & (latest.c.v == SubjectPluginConfig.version),
        )
        .where(SubjectPluginConfig.zip_data.is_(None))
        .order_by(SubjectPluginConfig.config["subjectCode"].astext)
    )
    report.subjects_without_archive = list(codes)
    return report


async def _run() -> int:
    storage = get_storage(get_settings())
    if storage is None:
        print("S3_ENDPOINT_URL is not set — nothing to migrate into.", file=sys.stderr)
        return 2
    async with get_session() as db:
        report = await migrate_uploads(db, storage, LEGACY_UPLOADS_DIR)
    print(f"copied: {report.copied}")
    print(f"already in storage: {report.present}")
    print(f"missing locally (lost before this migration): {len(report.missing)}")
    if report.missing:
        print("  submission ids: " + ", ".join(map(str, report.missing)))
    if report.subjects_without_archive:
        print(
            "subjects to re-apply (no stored archive): "
            + ", ".join(report.subjects_without_archive)
        )
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
