"""Edge / error-path integration tests for ConfigApplyService.apply().

Complements ``tests/integration/test_config_apply.py`` (which is NOT edited).
These target branches that file leaves uncovered:

* S3 upload failure → RuntimeError (and nothing committed).
* New-subject image upload → grid/main picture URLs set from url_map.
* Re-apply that *changes* an image filename → old S3 key removed (delete_file).
* Re-apply that updates every assignment field type (title/description/deadline/
  min_grade/max_grade/config/content_files) via the per-field apply path.
* Re-apply that *removes* a content file → old content S3 key removed.
* Deadline that fails ISO parsing → stored as NULL.
* An already-present content file on re-apply is skipped (no duplicate upload).

The DB-mutating apply() entry point is driven against the real Postgres
``db_session`` fixture; S3 is an ``AsyncMock`` so uploads/deletes are observable
without network I/O. Behaviour was read from ``services/config_apply.py``.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import yaml

from submissions_checker.db.models.enums import UserRole
from submissions_checker.db.models.subject import Subject
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.db.models.user import User
from submissions_checker.services.config_apply import ConfigApplyService

pytestmark = pytest.mark.asyncio


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_zip(config: dict[str, Any], extra_files: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("config.yml", yaml.safe_dump(config))
        for rel_path, data in (extra_files or {}).items():
            zf.writestr(rel_path, data)
    return buf.getvalue()


async def _make_owner(db: AsyncSession, username: str = "edgeteacher") -> User:
    user = User(username=username, password_hash="x", role=UserRole.TEACHER)
    db.add(user)
    await db.flush()
    return user


def _base_config() -> dict[str, Any]:
    return {
        "subjectCode": "edge101",
        "name": "Edge 101",
        "description": "Edge subject",
        "assignments": {
            "lab1": {
                "title": "Lab 1",
                "description": "First lab",
                "deadline": "2026-07-01T12:00:00",
                "min_grade": 0,
                "max_grade": 100,
                "review_mode": "tests_only",
            }
        },
    }


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload_file = AsyncMock(side_effect=lambda _p, key: f"https://cdn/{key}")
    storage.delete_file = AsyncMock()
    return storage


# ── S3 upload failure → RuntimeError, nothing persisted  362-364 ─────────────


async def test_s3_upload_failure_raises_runtime_error_and_persists_nothing(
    db_session: AsyncSession,
) -> None:
    owner = await _make_owner(db_session)
    await db_session.commit()

    storage = _mock_storage()
    storage.upload_file = AsyncMock(side_effect=RuntimeError("boom"))
    svc = ConfigApplyService(storage=storage)

    cfg = _base_config()
    cfg["gridPicture"] = "grid.png"
    zip_bytes = _make_zip(cfg, extra_files={"grid.png": b"\x89PNG fake"})

    with pytest.raises(RuntimeError, match="S3 upload failed"):
        await svc.apply(zip_bytes, owner_id=owner.id, db=db_session)

    # The DB transaction never reached commit → no subject row.
    await db_session.rollback()
    count = (
        await db_session.execute(
            select(func.count()).select_from(Subject).where(Subject.code == "edge101")
        )
    ).scalar_one()
    assert count == 0


# ── New-subject image upload sets picture URLs  220-224, 386-392 ─────────────


async def test_create_subject_uploads_and_sets_picture_urls(
    db_session: AsyncSession,
) -> None:
    owner = await _make_owner(db_session)
    await db_session.commit()
    storage = _mock_storage()
    svc = ConfigApplyService(storage=storage)

    cfg = _base_config()
    cfg["gridPicture"] = "grid.png"
    cfg["mainPicture"] = "main.png"
    zip_bytes = _make_zip(
        cfg, extra_files={"grid.png": b"\x89PNG grid", "main.png": b"\x89PNG main"}
    )

    result = await svc.apply(zip_bytes, owner_id=owner.id, db=db_session)
    assert result.subject_action == "created"

    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "edge101"))
    ).scalar_one()
    assert subject.grid_picture_url == "https://cdn/subjects/edge101/images/grid.png"
    assert subject.main_picture_url == "https://cdn/subjects/edge101/images/main.png"
    assert storage.upload_file.await_count == 2


# ── Changing an image filename removes the old S3 key  226-228, 453-459 ──────


async def test_reapply_changed_image_removes_old_s3_key(
    db_session: AsyncSession,
) -> None:
    owner = await _make_owner(db_session)
    await db_session.commit()
    storage = _mock_storage()
    svc = ConfigApplyService(storage=storage)

    cfg = _base_config()
    cfg["gridPicture"] = "old.png"
    await svc.apply(
        _make_zip(cfg, extra_files={"old.png": b"old"}),
        owner_id=owner.id, db=db_session,
    )

    storage.delete_file.reset_mock()
    cfg2 = _base_config()
    cfg2["gridPicture"] = "new.png"
    await svc.apply(
        _make_zip(cfg2, extra_files={"new.png": b"new"}),
        owner_id=owner.id, db=db_session,
    )

    # Old image key scheduled for best-effort delete.
    storage.delete_file.assert_awaited_once_with(
        "subjects/edge101/images/old.png"
    )
    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "edge101"))
    ).scalar_one()
    await db_session.refresh(subject)
    assert subject.grid_picture_url == "https://cdn/subjects/edge101/images/new.png"


# ── Update every assignment field type via the per-field apply path  ─────────
# Covers _apply_assignment_fields branches 503-516 and _apply_subject_fields
# image branches 480-491.


async def test_reapply_updates_all_assignment_field_types(
    db_session: AsyncSession,
) -> None:
    owner = await _make_owner(db_session)
    await db_session.commit()
    storage = _mock_storage()
    svc = ConfigApplyService(storage=storage)

    # v1: subject with a grid image + lab1 with one content file.
    cfg = _base_config()
    cfg["gridPicture"] = "old.png"
    cfg["assignments"]["lab1"]["contentFiles"] = [
        {"filename": "spec1.pdf", "displayName": "Spec v1"}
    ]
    await svc.apply(
        _make_zip(cfg, extra_files={
            "old.png": b"old",
            "assignments/lab1/spec1.pdf": b"%PDF v1",
        }),
        owner_id=owner.id, db=db_session,
    )

    # v2: change the grid image AND every lab1 field type, swap the content file.
    cfg2 = _base_config()
    cfg2["gridPicture"] = "new.png"
    cfg2["assignments"]["lab1"] = {
        "title": "Lab 1 v2",
        "description": "Updated desc",
        "deadline": "2026-09-09T09:00:00",
        "min_grade": 5,
        "max_grade": 95,
        "review_mode": "tests_then_teacher",  # config change
        "late_policy": "block",
        "contentFiles": [{"filename": "spec2.pdf", "displayName": "Spec v2"}],
    }
    storage.delete_file.reset_mock()
    result = await svc.apply(
        _make_zip(cfg2, extra_files={
            "new.png": b"new",
            "assignments/lab1/spec2.pdf": b"%PDF v2",
        }),
        owner_id=owner.id, db=db_session,
    )
    assert result.subject_action == "updated"

    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "edge101"))
    ).scalar_one()
    await db_session.refresh(subject)
    assert subject.grid_picture_url == "https://cdn/subjects/edge101/images/new.png"

    a = (
        await db_session.execute(
            select(SubjectsAssignment).where(
                SubjectsAssignment.subject_id == subject.id,
                SubjectsAssignment.code == "lab1",
            )
        )
    ).scalar_one()
    await db_session.refresh(a)
    assert a.title == "Lab 1 v2"
    assert a.description == "Updated desc"
    assert a.deadline == datetime(2026, 9, 9, 9, 0, 0, tzinfo=UTC)
    assert a.min_grade == 5
    assert a.max_grade == 95
    assert a.config == {"review_mode": "tests_then_teacher", "late_policy": "block"}
    assert a.content_files == [
        {
            "url": "https://cdn/subjects/edge101/assignments/lab1/spec2.pdf",
            "display_name": "Spec v2",
            "filename": "spec2.pdf",
        }
    ]

    # Old content file + old image both removed from S3 (best-effort).
    deleted_keys = {c.args[0] for c in storage.delete_file.await_args_list}
    assert "subjects/edge101/assignments/lab1/spec1.pdf" in deleted_keys
    assert "subjects/edge101/images/old.png" in deleted_keys


# ── Removing a content file on re-apply removes its S3 key  248-253 ──────────


async def test_reapply_removes_content_file_s3_key(db_session: AsyncSession) -> None:
    owner = await _make_owner(db_session)
    await db_session.commit()
    storage = _mock_storage()
    svc = ConfigApplyService(storage=storage)

    cfg = _base_config()
    cfg["assignments"]["lab1"]["contentFiles"] = [
        {"filename": "doc.pdf", "displayName": "Doc"}
    ]
    await svc.apply(
        _make_zip(cfg, extra_files={"assignments/lab1/doc.pdf": b"%PDF"}),
        owner_id=owner.id, db=db_session,
    )

    storage.delete_file.reset_mock()
    cfg2 = _base_config()  # lab1 has no contentFiles now
    await svc.apply(_make_zip(cfg2), owner_id=owner.id, db=db_session)

    storage.delete_file.assert_awaited_once_with(
        "subjects/edge101/assignments/lab1/doc.pdf"
    )
    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "edge101"))
    ).scalar_one()
    a = (
        await db_session.execute(
            select(SubjectsAssignment).where(SubjectsAssignment.subject_id == subject.id)
        )
    ).scalar_one()
    await db_session.refresh(a)
    assert a.content_files in (None, [])


# ── Unchanged content file is not re-uploaded on re-apply  311-315 ───────────


async def test_reapply_existing_content_file_not_reuploaded(
    db_session: AsyncSession,
) -> None:
    owner = await _make_owner(db_session)
    await db_session.commit()
    storage = _mock_storage()
    svc = ConfigApplyService(storage=storage)

    cfg = _base_config()
    cfg["assignments"]["lab1"]["contentFiles"] = [
        {"filename": "keep.pdf", "displayName": "Keep"}
    ]
    extras = {"assignments/lab1/keep.pdf": b"%PDF keep"}
    await svc.apply(_make_zip(cfg, extras), owner_id=owner.id, db=db_session)
    assert storage.upload_file.await_count == 1

    # Re-apply with the SAME content file but a changed title so the plan still
    # registers an assignment update → _collect_new_content_files sees keep.pdf
    # already present (line 314-315) and does not schedule another upload.
    storage.upload_file.reset_mock()
    cfg2 = _base_config()
    cfg2["assignments"]["lab1"]["title"] = "Lab 1 Renamed"
    cfg2["assignments"]["lab1"]["contentFiles"] = [
        {"filename": "keep.pdf", "displayName": "Keep"}
    ]
    await svc.apply(_make_zip(cfg2, extras), owner_id=owner.id, db=db_session)
    assert storage.upload_file.await_count == 0


# ── Changing the MAIN picture on re-apply  486-491 ───────────────────────────


async def test_reapply_changes_main_picture_url(db_session: AsyncSession) -> None:
    owner = await _make_owner(db_session)
    await db_session.commit()
    storage = _mock_storage()
    svc = ConfigApplyService(storage=storage)

    cfg = _base_config()
    cfg["mainPicture"] = "main_old.png"
    await svc.apply(
        _make_zip(cfg, extra_files={"main_old.png": b"old"}),
        owner_id=owner.id, db=db_session,
    )

    cfg2 = _base_config()
    cfg2["mainPicture"] = "main_new.png"
    await svc.apply(
        _make_zip(cfg2, extra_files={"main_new.png": b"new"}),
        owner_id=owner.id, db=db_session,
    )

    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "edge101"))
    ).scalar_one()
    await db_session.refresh(subject)
    assert subject.main_picture_url == "https://cdn/subjects/edge101/images/main_new.png"


# ── S3 cleanup delete failure is swallowed (best-effort)  455-459 ────────────


async def test_s3_delete_failure_is_swallowed(db_session: AsyncSession) -> None:
    owner = await _make_owner(db_session)
    await db_session.commit()
    storage = _mock_storage()
    svc = ConfigApplyService(storage=storage)

    cfg = _base_config()
    cfg["gridPicture"] = "g_old.png"
    await svc.apply(
        _make_zip(cfg, extra_files={"g_old.png": b"old"}),
        owner_id=owner.id, db=db_session,
    )

    # delete_file raises on cleanup — apply() must still succeed (logged warning).
    storage.delete_file = AsyncMock(side_effect=RuntimeError("s3 down"))
    cfg2 = _base_config()
    cfg2["gridPicture"] = "g_new.png"
    result = await svc.apply(
        _make_zip(cfg2, extra_files={"g_new.png": b"new"}),
        owner_id=owner.id, db=db_session,
    )
    # Despite the delete failure, the apply committed and reports updated.
    assert result.changed is True
    assert result.subject_action == "updated"
    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "edge101"))
    ).scalar_one()
    await db_session.refresh(subject)
    assert subject.grid_picture_url == "https://cdn/subjects/edge101/images/g_new.png"


# ── Content-file entry without a filename is skipped  563-564 ────────────────


async def test_content_file_without_filename_skipped(db_session: AsyncSession) -> None:
    owner = await _make_owner(db_session)
    await db_session.commit()
    storage = _mock_storage()
    svc = ConfigApplyService(storage=storage)

    cfg = _base_config()
    # One valid content file + one malformed entry (no filename) → the malformed
    # one is skipped by _build_content_files (line 563-564).
    cfg["assignments"]["lab1"]["contentFiles"] = [
        {"filename": "ok.pdf", "displayName": "OK"},
        {"displayName": "no filename here"},
    ]
    await svc.apply(
        _make_zip(cfg, extra_files={"assignments/lab1/ok.pdf": b"%PDF ok"}),
        owner_id=owner.id, db=db_session,
    )

    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "edge101"))
    ).scalar_one()
    a = (
        await db_session.execute(
            select(SubjectsAssignment).where(SubjectsAssignment.subject_id == subject.id)
        )
    ).scalar_one()
    await db_session.refresh(a)
    # Only the well-formed content file made it through.
    assert a.content_files == [
        {
            "url": "https://cdn/subjects/edge101/assignments/lab1/ok.pdf",
            "display_name": "OK",
            "filename": "ok.pdf",
        }
    ]


# ── Malformed deadline parses to NULL  570-577 ───────────────────────────────


async def test_apply_invalid_deadline_stored_as_null(db_session: AsyncSession) -> None:
    owner = await _make_owner(db_session)
    await db_session.commit()
    svc = ConfigApplyService(storage=None)

    cfg = _base_config()
    cfg["assignments"]["lab1"]["deadline"] = "not-a-date"
    await svc.apply(_make_zip(cfg), owner_id=owner.id, db=db_session)

    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "edge101"))
    ).scalar_one()
    a = (
        await db_session.execute(
            select(SubjectsAssignment).where(SubjectsAssignment.subject_id == subject.id)
        )
    ).scalar_one()
    assert a.deadline is None
