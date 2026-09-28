"""Backfill of legacy local submission ZIPs into object storage."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.cli.migrate_uploads import migrate_uploads
from tests.integration.test_worker_tasks import _seed_check_submission
from tests.storage_fake import FakeStorage


@pytest.mark.asyncio
async def test_copies_missing_skips_present_reports_absent(
    db_session: AsyncSession, tmp_path
) -> None:
    await _seed_check_submission(db_session, "mu-a", saved_as="a.zip")
    await _seed_check_submission(db_session, "mu-b", saved_as="b.zip", zip_data=None)
    c = await _seed_check_submission(db_session, "mu-c", saved_as="c.zip")
    (tmp_path / "a.zip").write_bytes(b"A")
    storage = FakeStorage()
    storage.objects["submissions/b.zip"] = b"B"

    report = await migrate_uploads(db_session, storage, tmp_path)

    assert storage.objects["submissions/a.zip"] == b"A"
    assert (report.copied, report.present) == (1, 1)
    assert report.missing == [c.id]
    assert "sub" in report.subjects_without_archive  # b's subject has zip_data NULL

    again = await migrate_uploads(db_session, storage, tmp_path)
    assert (again.copied, again.present) == (0, 2)
