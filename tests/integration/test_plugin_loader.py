"""Integration tests for PluginLoader.load_all().

PluginLoader scans a directory of plugin folders (each with a config.yml at its
root) and upserts Subject / SubjectsAssignment / SubjectPluginConfig rows on
startup. These tests point it at tmp_path-built plugin dirs and assert the real
DB side effects against the Postgres testcontainer (db_session fixture). S3 is
optional and disabled (storage=None) unless a test specifically mocks it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.db.models.enums import EntityType
from submissions_checker.db.models.group import Group
from submissions_checker.db.models.student import Student
from submissions_checker.db.models.student_assignment import StudentAssignment
from submissions_checker.db.models.subject import Subject, SubjectsStudents
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.services.plugin_loader import PluginLoader

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_plugin(root: Path, name: str, config: dict[str, Any]) -> Path:
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "config.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return plugin_dir


def _base_config() -> dict[str, Any]:
    return {
        "subjectCode": "py101",
        "name": "Python 101",
        "description": "Intro to Python",
        "assignments": {
            "lab1": {
                "title": "Lab 1",
                "description": "First lab",
                "deadline": "2026-07-01T09:00:00",
                "min_grade": 0,
                "max_grade": 100,
                "review_mode": "tests_only",
                "late_policy": "block",
                "sandbox": {"image": "python:3.12-slim"},
            }
        },
    }


async def _make_student(db: AsyncSession, email: str = "s@example.com") -> Student:
    group = Group(name=f"grp-{email}", type=EntityType.REAL)
    db.add(group)
    await db.flush()
    student = Student(
        group_id=group.id, email=email, full_name="S", type=EntityType.REAL
    )
    db.add(student)
    await db.flush()
    return student


# ---------------------------------------------------------------------------
# 1. Valid plugin loads/registers
# ---------------------------------------------------------------------------


async def test_load_all_creates_subject_assignment_and_config(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    _write_plugin(tmp_path, "py101", _base_config())

    await PluginLoader().load_all(tmp_path, db_session, storage=None)
    await db_session.commit()

    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "py101"))
    ).scalar_one()
    assert subject.name == "Python 101"
    assert subject.description == "Intro to Python"
    # Startup loader does not set ownership
    assert subject.owner_id is None

    a = (
        await db_session.execute(
            select(SubjectsAssignment).where(SubjectsAssignment.subject_id == subject.id)
        )
    ).scalar_one()
    assert a.code == "lab1"
    assert a.title == "Lab 1"
    assert a.deadline == datetime(2026, 7, 1, 9, 0, 0, tzinfo=UTC)
    assert a.config == {
        "review_mode": "tests_only",
        "late_policy": "block",
        "sandbox": {"image": "python:3.12-slim"},
    }

    cfg = (
        await db_session.execute(
            select(SubjectPluginConfig).where(SubjectPluginConfig.subject_id == subject.id)
        )
    ).scalar_one()
    assert cfg.version == 1
    assert cfg.loaded_from is not None
    assert cfg.loaded_from.endswith("config.yml")
    assert cfg.zip_data is None  # startup loader stores no ZIP bytes


async def test_load_all_idempotent_on_unchanged_config(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    _write_plugin(tmp_path, "py101", _base_config())
    loader = PluginLoader()

    await loader.load_all(tmp_path, db_session, storage=None)
    await db_session.commit()
    await loader.load_all(tmp_path, db_session, storage=None)
    await db_session.commit()

    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "py101"))
    ).scalar_one()
    version_count = (
        await db_session.execute(
            select(func.count())
            .select_from(SubjectPluginConfig)
            .where(SubjectPluginConfig.subject_id == subject.id)
        )
    ).scalar_one()
    # Same content_hash -> no new version row inserted
    assert version_count == 1


async def test_load_all_upserts_changed_config_new_version(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    plugin_dir = _write_plugin(tmp_path, "py101", _base_config())
    loader = PluginLoader()
    await loader.load_all(tmp_path, db_session, storage=None)
    await db_session.commit()

    # Mutate the config and re-run
    cfg2 = _base_config()
    cfg2["name"] = "Python 101 v2"
    cfg2["assignments"]["lab1"]["title"] = "Lab 1 Updated"
    cfg2["assignments"]["lab2"] = {"title": "Lab 2", "min_grade": 0, "max_grade": 50}
    (plugin_dir / "config.yml").write_text(yaml.safe_dump(cfg2), encoding="utf-8")

    await loader.load_all(tmp_path, db_session, storage=None)
    await db_session.commit()

    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "py101"))
    ).scalar_one()
    await db_session.refresh(subject)
    assert subject.name == "Python 101 v2"

    versions = (
        await db_session.execute(
            select(SubjectPluginConfig.version)
            .where(SubjectPluginConfig.subject_id == subject.id)
            .order_by(SubjectPluginConfig.version)
        )
    ).scalars().all()
    assert versions == [1, 2]

    assignments = {
        a.code: a
        for a in (
            await db_session.execute(
                select(SubjectsAssignment).where(
                    SubjectsAssignment.subject_id == subject.id
                )
            )
        ).scalars().all()
    }
    assert set(assignments) == {"lab1", "lab2"}
    assert assignments["lab1"].title == "Lab 1 Updated"
    assert assignments["lab2"].max_grade == 50


async def test_load_all_multiple_plugins(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    _write_plugin(tmp_path, "a_py", {"subjectCode": "py", "name": "Py", "assignments": {}})
    _write_plugin(tmp_path, "b_cpp", {"subjectCode": "cpp", "name": "Cpp", "assignments": {}})

    await PluginLoader().load_all(tmp_path, db_session, storage=None)
    await db_session.commit()

    codes = (
        await db_session.execute(select(Subject.code).order_by(Subject.code))
    ).scalars().all()
    assert codes == ["cpp", "py"]


# ---------------------------------------------------------------------------
# 2. Malformed / incomplete plugin dirs
# ---------------------------------------------------------------------------


async def test_load_all_skips_dirs_without_config(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    # A directory without config.yml is silently skipped
    (tmp_path / "no_config").mkdir()
    (tmp_path / "no_config" / "readme.txt").write_text("nothing")
    # A valid one alongside it
    _write_plugin(tmp_path, "py101", _base_config())

    await PluginLoader().load_all(tmp_path, db_session, storage=None)
    await db_session.commit()

    codes = (await db_session.execute(select(Subject.code))).scalars().all()
    assert codes == ["py101"]


async def test_load_all_missing_dir_is_noop(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    missing = tmp_path / "does_not_exist"
    # Should not raise
    await PluginLoader().load_all(missing, db_session, storage=None)
    await db_session.commit()
    count = (
        await db_session.execute(select(func.count()).select_from(Subject))
    ).scalar_one()
    assert count == 0


async def test_load_all_isolates_failures_per_plugin(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    # Bad plugin: config.yml present but missing required 'subjectCode' key.
    # load_all wraps _load_plugin in try/except and logs the error, so the good
    # plugin must still load. NOTE: a KeyError mid-_load_plugin can leave pending
    # ORM state on the session; we let load_all run and assert the good subject
    # ends up persisted.
    _write_plugin(tmp_path, "a_bad", {"name": "No Code", "assignments": {}})
    _write_plugin(tmp_path, "z_good", _base_config())

    await PluginLoader().load_all(tmp_path, db_session, storage=None)
    await db_session.commit()

    codes = (await db_session.execute(select(Subject.code))).scalars().all()
    assert "py101" in codes
    assert "No Code" not in codes


async def test_load_all_skips_plain_files_in_dir(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    # A loose file (not a dir) at the top level must be ignored.
    (tmp_path / "stray.txt").write_text("hi")
    _write_plugin(tmp_path, "py101", _base_config())

    await PluginLoader().load_all(tmp_path, db_session, storage=None)
    await db_session.commit()

    codes = (await db_session.execute(select(Subject.code))).scalars().all()
    assert codes == ["py101"]


# ---------------------------------------------------------------------------
# 3. Storage interactions
# ---------------------------------------------------------------------------


async def test_load_all_storage_none_skips_uploads(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    cfg = _base_config()
    cfg["gridPicture"] = "grid.png"
    cfg["assignments"]["lab1"]["contentFiles"] = [
        {"filename": "spec.pdf", "displayName": "Spec"}
    ]
    plugin_dir = _write_plugin(tmp_path, "py101", cfg)
    (plugin_dir / "grid.png").write_bytes(b"png")
    (plugin_dir / "assignments" / "lab1").mkdir(parents=True)
    (plugin_dir / "assignments" / "lab1" / "spec.pdf").write_bytes(b"%PDF")

    # storage=None -> no uploads attempted, content_files stays unset
    await PluginLoader().load_all(tmp_path, db_session, storage=None)
    await db_session.commit()

    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "py101"))
    ).scalar_one()
    assert subject.grid_picture_url is None
    a = (
        await db_session.execute(
            select(SubjectsAssignment).where(SubjectsAssignment.subject_id == subject.id)
        )
    ).scalar_one()
    assert a.content_files is None


async def test_load_all_with_storage_uploads_images_and_content(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    cfg = _base_config()
    cfg["gridPicture"] = "grid.png"
    cfg["assignments"]["lab1"]["contentFiles"] = [
        {"filename": "spec.pdf", "displayName": "Spec"}
    ]
    plugin_dir = _write_plugin(tmp_path, "py101", cfg)
    (plugin_dir / "grid.png").write_bytes(b"png")
    (plugin_dir / "assignments" / "lab1").mkdir(parents=True)
    (plugin_dir / "assignments" / "lab1" / "spec.pdf").write_bytes(b"%PDF")

    storage = AsyncMock()
    storage.upload_file = AsyncMock(
        side_effect=lambda local, key: f"https://cdn/{key}"
    )

    await PluginLoader().load_all(tmp_path, db_session, storage=storage)
    await db_session.commit()

    # Two uploads: the grid image and the content file
    assert storage.upload_file.await_count == 2

    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "py101"))
    ).scalar_one()
    await db_session.refresh(subject)
    assert subject.grid_picture_url == "https://cdn/subjects/py101/images/grid.png"
    a = (
        await db_session.execute(
            select(SubjectsAssignment).where(SubjectsAssignment.subject_id == subject.id)
        )
    ).scalar_one()
    assert a.content_files == [
        {
            "url": "https://cdn/subjects/py101/assignments/lab1/spec.pdf",
            "display_name": "Spec",
            "filename": "spec.pdf",
        }
    ]


async def test_load_all_storage_missing_image_file_is_tolerated(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    cfg = _base_config()
    cfg["gridPicture"] = "missing.png"  # referenced but not on disk
    _write_plugin(tmp_path, "py101", cfg)

    storage = AsyncMock()
    storage.upload_file = AsyncMock(return_value="https://cdn/x")

    await PluginLoader().load_all(tmp_path, db_session, storage=storage)
    await db_session.commit()

    storage.upload_file.assert_not_awaited()
    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "py101"))
    ).scalar_one()
    assert subject.grid_picture_url is None


# ---------------------------------------------------------------------------
# 4. Student fan-out on new assignment
# ---------------------------------------------------------------------------


async def test_new_assignment_fans_out_to_enrolled_students(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    # First load a subject with no assignments, enroll a student, then add one.
    _write_plugin(tmp_path, "py101", {"subjectCode": "py101", "name": "Py", "assignments": {}})
    loader = PluginLoader()
    await loader.load_all(tmp_path, db_session, storage=None)
    await db_session.commit()

    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "py101"))
    ).scalar_one()
    student = await _make_student(db_session)
    db_session.add(SubjectsStudents(subject_id=subject.id, student_id=student.id))
    await db_session.commit()

    # Now add lab1
    (tmp_path / "py101" / "config.yml").write_text(
        yaml.safe_dump(_base_config()), encoding="utf-8"
    )
    await loader.load_all(tmp_path, db_session, storage=None)
    await db_session.commit()

    sa = (
        await db_session.execute(
            select(SubjectsAssignment).where(SubjectsAssignment.subject_id == subject.id)
        )
    ).scalar_one()
    sas = (
        await db_session.execute(
            select(StudentAssignment).where(
                StudentAssignment.subjects_assignment_id == sa.id
            )
        )
    ).scalars().all()
    assert len(sas) == 1
    assert sas[0].student_id == student.id
