"""Integration tests for the ZIP-driven ConfigApplyService.apply() pipeline.

These exercise the real DB-backed apply() path end to end against a Postgres
testcontainer (via the function-scoped ``db_session`` fixture in
tests/conftest.py). S3 storage is mocked / disabled (storage=None) so no network
I/O occurs; we assert the real Subject / SubjectsAssignment / SubjectPluginConfig
rows and field mappings that apply() produces.

The pure side-effect-free helpers (_parse_deadline, _diff_assignment,
_build_assignment_config, _build_content_files, _compute_plan) are already
covered in tests/unit/test_config_apply_helpers.py — this file deliberately does
NOT duplicate them and instead drives the DB-mutating apply() entry point.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.db.models.enums import (
    EntityType,
    SubjectStatus,
    UserRole,
)
from submissions_checker.db.models.group import Group
from submissions_checker.db.models.student import Student
from submissions_checker.db.models.student_assignment import StudentAssignment
from submissions_checker.db.models.subject import Subject, SubjectsStudents
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.db.models.user import User
from submissions_checker.services.config_apply import ConfigApplyService

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_zip(config: dict[str, Any], extra_files: dict[str, bytes] | None = None) -> bytes:
    """Build an in-memory ZIP with config.yml at the root plus optional files."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("config.yml", yaml.safe_dump(config))
        for rel_path, data in (extra_files or {}).items():
            zf.writestr(rel_path, data)
    return buf.getvalue()


async def _make_owner(db: AsyncSession, username: str = "teacher1") -> User:
    user = User(username=username, password_hash="x", role=UserRole.TEACHER)
    db.add(user)
    await db.flush()
    return user


async def _make_student(db: AsyncSession, email: str = "s1@example.com") -> Student:
    group = Group(name=f"grp-{email}", type=EntityType.REAL)
    db.add(group)
    await db.flush()
    student = Student(
        group_id=group.id,
        email=email,
        full_name="Student One",
        type=EntityType.REAL,
    )
    db.add(student)
    await db.flush()
    return student


def _base_config() -> dict[str, Any]:
    return {
        "subjectCode": "demo101",
        "name": "Demo 101",
        "description": "A demo subject",
        "assignments": {
            "lab1": {
                "title": "Lab 1",
                "description": "First lab",
                "deadline": "2026-07-01T12:00:00",
                "min_grade": 0,
                "max_grade": 100,
                "review_mode": "tests_only",
                "late_policy": "allow",
                "max_submissions": 5,
                "sandbox": {"image": "python:3.12-slim", "tool": "python3"},
            }
        },
    }


# ---------------------------------------------------------------------------
# 1. Fresh apply CREATES Subject + assignments + versioned plugin config
# ---------------------------------------------------------------------------


async def test_fresh_apply_creates_subject_assignment_and_config(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    owner = await _make_owner(db_session)
    await db_session.commit()

    svc = ConfigApplyService(storage=None, plugins_dir=tmp_path)
    result = await svc.apply(_make_zip(_base_config()), owner_id=owner.id, db=db_session)

    assert result.changed is True
    assert result.subject_action == "created"
    assert result.subject_name == "Demo 101"

    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "demo101"))
    ).scalar_one()
    assert subject.name == "Demo 101"
    assert subject.description == "A demo subject"
    assert subject.owner_id == owner.id
    assert subject.status == SubjectStatus.ACTIVE

    assignments = (
        (
            await db_session.execute(
                select(SubjectsAssignment).where(SubjectsAssignment.subject_id == subject.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(assignments) == 1
    a = assignments[0]
    assert a.code == "lab1"
    assert a.title == "Lab 1"
    assert a.description == "First lab"
    assert a.deadline == datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
    assert a.min_grade == 0
    assert a.max_grade == 100
    # config JSONB collects only the known assignment keys
    assert a.config == {
        "review_mode": "tests_only",
        "late_policy": "allow",
        "max_submissions": 5,
        "sandbox": {"image": "python:3.12-slim", "tool": "python3"},
    }

    cfg = (
        await db_session.execute(
            select(SubjectPluginConfig).where(SubjectPluginConfig.subject_id == subject.id)
        )
    ).scalar_one()
    assert cfg.version == 1
    assert cfg.content_hash and len(cfg.content_hash) == 64
    assert cfg.config["subjectCode"] == "demo101"
    assert cfg.zip_data is not None  # ZIP bytes stored for UI uploads
    assert cfg.loaded_from is None


# ---------------------------------------------------------------------------
# 2. Re-apply upserts by code: changes propagate, new version, single ACTIVE
# ---------------------------------------------------------------------------


async def test_reapply_updates_subject_assignments_and_bumps_version(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    owner = await _make_owner(db_session)
    await db_session.commit()
    svc = ConfigApplyService(storage=None, plugins_dir=tmp_path)

    await svc.apply(_make_zip(_base_config()), owner_id=owner.id, db=db_session)
    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "demo101"))
    ).scalar_one()
    subject_id = subject.id

    # Updated config: rename subject, modify lab1, add lab2, remove nothing
    cfg2 = _base_config()
    cfg2["name"] = "Demo 101 (v2)"
    cfg2["assignments"]["lab1"]["title"] = "Lab 1 Renamed"
    cfg2["assignments"]["lab1"]["max_grade"] = 90
    cfg2["assignments"]["lab2"] = {
        "title": "Lab 2",
        "min_grade": 10,
        "max_grade": 80,
        "review_mode": "tests_then_teacher",
    }

    result = await svc.apply(_make_zip(cfg2), owner_id=owner.id, db=db_session)
    assert result.changed is True
    assert result.subject_action == "updated"

    # Same subject row (upsert by code), not a new one
    all_subjects = (
        (await db_session.execute(select(Subject).where(Subject.code == "demo101"))).scalars().all()
    )
    assert len(all_subjects) == 1
    assert all_subjects[0].id == subject_id
    await db_session.refresh(all_subjects[0])
    assert all_subjects[0].name == "Demo 101 (v2)"

    # Assignment changes propagated
    assignments = {
        a.code: a
        for a in (
            await db_session.execute(
                select(SubjectsAssignment).where(SubjectsAssignment.subject_id == subject_id)
            )
        )
        .scalars()
        .all()
    }
    assert set(assignments) == {"lab1", "lab2"}
    assert assignments["lab1"].title == "Lab 1 Renamed"
    assert assignments["lab1"].max_grade == 90
    assert assignments["lab2"].title == "Lab 2"
    assert assignments["lab2"].min_grade == 10
    assert assignments["lab2"].config["review_mode"] == "tests_then_teacher"

    # A second plugin-config version was created
    versions = (
        (
            await db_session.execute(
                select(SubjectPluginConfig.version)
                .where(SubjectPluginConfig.subject_id == subject_id)
                .order_by(SubjectPluginConfig.version)
            )
        )
        .scalars()
        .all()
    )
    assert versions == [1, 2]

    # Exactly one ACTIVE subject for this code (partial unique index honoured)
    active_count = (
        await db_session.execute(
            select(func.count())
            .select_from(Subject)
            .where(Subject.code == "demo101", Subject.status == SubjectStatus.ACTIVE)
        )
    ).scalar_one()
    assert active_count == 1


async def test_reapply_removes_deleted_assignment(db_session: AsyncSession, tmp_path: Path) -> None:
    owner = await _make_owner(db_session)
    await db_session.commit()
    svc = ConfigApplyService(storage=None, plugins_dir=tmp_path)

    cfg = _base_config()
    cfg["assignments"]["lab2"] = {"title": "Lab 2", "min_grade": 0, "max_grade": 100}
    await svc.apply(_make_zip(cfg), owner_id=owner.id, db=db_session)

    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "demo101"))
    ).scalar_one()
    assert (
        await db_session.execute(
            select(func.count())
            .select_from(SubjectsAssignment)
            .where(SubjectsAssignment.subject_id == subject.id)
        )
    ).scalar_one() == 2

    # Drop lab2 on re-apply
    cfg2 = _base_config()  # only lab1
    await svc.apply(_make_zip(cfg2), owner_id=owner.id, db=db_session)

    remaining = (
        (
            await db_session.execute(
                select(SubjectsAssignment.code).where(SubjectsAssignment.subject_id == subject.id)
            )
        )
        .scalars()
        .all()
    )
    assert remaining == ["lab1"]


async def test_reapply_identical_zip_is_unchanged(db_session: AsyncSession, tmp_path: Path) -> None:
    owner = await _make_owner(db_session)
    await db_session.commit()
    svc = ConfigApplyService(storage=None, plugins_dir=tmp_path)

    zip_bytes = _make_zip(_base_config())
    await svc.apply(zip_bytes, owner_id=owner.id, db=db_session)

    # Exact same bytes -> dedup by content_hash, no new version
    result = await svc.apply(zip_bytes, owner_id=owner.id, db=db_session)
    assert result.changed is False
    assert result.subject_action == "unchanged"

    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "demo101"))
    ).scalar_one()
    version_count = (
        await db_session.execute(
            select(func.count())
            .select_from(SubjectPluginConfig)
            .where(SubjectPluginConfig.subject_id == subject.id)
        )
    ).scalar_one()
    assert version_count == 1


# ---------------------------------------------------------------------------
# 3. owner_id semantics
# ---------------------------------------------------------------------------


async def test_owner_id_set_on_create(db_session: AsyncSession, tmp_path: Path) -> None:
    owner = await _make_owner(db_session, "ownerA")
    await db_session.commit()
    svc = ConfigApplyService(storage=None, plugins_dir=tmp_path)
    await svc.apply(_make_zip(_base_config()), owner_id=owner.id, db=db_session)

    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "demo101"))
    ).scalar_one()
    assert subject.owner_id == owner.id


async def test_non_owner_cannot_reapply(db_session: AsyncSession, tmp_path: Path) -> None:
    owner = await _make_owner(db_session, "ownerA")
    intruder = await _make_owner(db_session, "ownerB")
    await db_session.commit()
    svc = ConfigApplyService(storage=None, plugins_dir=tmp_path)

    await svc.apply(_make_zip(_base_config()), owner_id=owner.id, db=db_session)

    cfg2 = _base_config()
    cfg2["name"] = "Hijacked"
    with pytest.raises(PermissionError):
        await svc.apply(_make_zip(cfg2), owner_id=intruder.id, db=db_session)

    # Owner unchanged, name unchanged
    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "demo101"))
    ).scalar_one()
    assert subject.owner_id == owner.id
    assert subject.name == "Demo 101"


async def test_create_then_enrolled_students_get_student_assignments(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    owner = await _make_owner(db_session)
    student = await _make_student(db_session)
    await db_session.commit()
    svc = ConfigApplyService(storage=None, plugins_dir=tmp_path)

    # Create the subject first (no assignments yet) so we can enroll a student,
    # then add an assignment on re-apply and assert StudentAssignment fan-out.
    cfg = _base_config()
    del cfg["assignments"]["lab1"]
    cfg["assignments"] = {}
    await svc.apply(_make_zip(cfg), owner_id=owner.id, db=db_session)

    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "demo101"))
    ).scalar_one()
    db_session.add(SubjectsStudents(subject_id=subject.id, student_id=student.id))
    await db_session.commit()

    # Now add lab1 -> assignments_to_create path should fan out to the student
    await svc.apply(_make_zip(_base_config()), owner_id=owner.id, db=db_session)

    sa = (
        await db_session.execute(
            select(SubjectsAssignment).where(SubjectsAssignment.subject_id == subject.id)
        )
    ).scalar_one()
    student_assignments = (
        (
            await db_session.execute(
                select(StudentAssignment).where(StudentAssignment.subjects_assignment_id == sa.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(student_assignments) == 1
    assert student_assignments[0].student_id == student.id


# ---------------------------------------------------------------------------
# 4. Edge cases: invalid input, content files, deadlines
# ---------------------------------------------------------------------------


async def test_apply_rejects_non_zip(db_session: AsyncSession, tmp_path: Path) -> None:
    owner = await _make_owner(db_session)
    await db_session.commit()
    svc = ConfigApplyService(storage=None, plugins_dir=tmp_path)
    with pytest.raises(ValueError, match="not a valid ZIP"):
        await svc.apply(b"this is not a zip", owner_id=owner.id, db=db_session)


async def test_apply_rejects_missing_config_yml(db_session: AsyncSession, tmp_path: Path) -> None:
    owner = await _make_owner(db_session)
    await db_session.commit()
    svc = ConfigApplyService(storage=None, plugins_dir=tmp_path)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "no config here")

    with pytest.raises(ValueError, match="must contain config.yml"):
        await svc.apply(buf.getvalue(), owner_id=owner.id, db=db_session)


async def test_apply_rejects_empty_subject_code(db_session: AsyncSession, tmp_path: Path) -> None:
    owner = await _make_owner(db_session)
    await db_session.commit()
    svc = ConfigApplyService(storage=None, plugins_dir=tmp_path)
    cfg = _base_config()
    cfg["subjectCode"] = ""
    with pytest.raises(ValueError, match="subjectCode"):
        await svc.apply(_make_zip(cfg), owner_id=owner.id, db=db_session)


async def test_apply_rejects_oversize_zip(db_session: AsyncSession, tmp_path: Path) -> None:
    owner = await _make_owner(db_session)
    await db_session.commit()
    svc = ConfigApplyService(storage=None, plugins_dir=tmp_path)
    big = b"\x00" * (50 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="50 MB limit"):
        await svc.apply(big, owner_id=owner.id, db=db_session)


async def test_apply_with_storage_uploads_content_files(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    owner = await _make_owner(db_session)
    await db_session.commit()

    storage = AsyncMock()
    storage.upload_file = AsyncMock(return_value="https://cdn/spec.pdf")
    storage.delete_file = AsyncMock()
    svc = ConfigApplyService(storage=storage, plugins_dir=tmp_path)

    cfg = _base_config()
    cfg["assignments"]["lab1"]["contentFiles"] = [{"filename": "spec.pdf", "displayName": "Spec"}]
    zip_bytes = _make_zip(cfg, extra_files={"assignments/lab1/spec.pdf": b"%PDF-1.4 fake"})

    await svc.apply(zip_bytes, owner_id=owner.id, db=db_session)

    storage.upload_file.assert_awaited_once()
    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "demo101"))
    ).scalar_one()
    a = (
        await db_session.execute(
            select(SubjectsAssignment).where(SubjectsAssignment.subject_id == subject.id)
        )
    ).scalar_one()
    assert a.content_files == [
        {
            "url": "https://cdn/spec.pdf",
            "display_name": "Spec",
            "filename": "spec.pdf",
        }
    ]


async def test_apply_null_deadline_when_absent(db_session: AsyncSession, tmp_path: Path) -> None:
    owner = await _make_owner(db_session)
    await db_session.commit()
    svc = ConfigApplyService(storage=None, plugins_dir=tmp_path)
    cfg = _base_config()
    del cfg["assignments"]["lab1"]["deadline"]
    await svc.apply(_make_zip(cfg), owner_id=owner.id, db=db_session)

    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "demo101"))
    ).scalar_one()
    a = (
        await db_session.execute(
            select(SubjectsAssignment).where(SubjectsAssignment.subject_id == subject.id)
        )
    ).scalar_one()
    assert a.deadline is None


# ---------------------------------------------------------------------------
# Plugin tree extraction: the ZIP upload is the only way checker code reaches disk
# ---------------------------------------------------------------------------


async def test_fresh_apply_extracts_full_zip_tree_to_plugins_dir(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    owner = await _make_owner(db_session)
    await db_session.commit()
    svc = ConfigApplyService(storage=None, plugins_dir=tmp_path)

    zip_bytes = _make_zip(
        _base_config(),
        extra_files={
            "assignments/lab1/check.py": b"print('check')",
            "assignments/lab1/fixtures/input.txt": b"seed data",
        },
    )
    await svc.apply(zip_bytes, owner_id=owner.id, db=db_session)

    subject_dir = tmp_path / "demo101"
    assert (subject_dir / "config.yml").is_file()
    assert (subject_dir / "assignments" / "lab1" / "check.py").read_bytes() == b"print('check')"
    assert (subject_dir / "assignments" / "lab1" / "fixtures" / "input.txt").is_file()


async def test_reapply_removes_stale_files_from_plugins_dir(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    owner = await _make_owner(db_session)
    await db_session.commit()
    svc = ConfigApplyService(storage=None, plugins_dir=tmp_path)

    cfg = _base_config()
    await svc.apply(
        _make_zip(cfg, extra_files={"assignments/lab1/check.py": b"v1", "old_helper.py": b"stale"}),
        owner_id=owner.id,
        db=db_session,
    )
    subject_dir = tmp_path / "demo101"
    assert (subject_dir / "old_helper.py").is_file()

    cfg["description"] = "changed so the hash differs"
    await svc.apply(
        _make_zip(cfg, extra_files={"assignments/lab1/check.py": b"v2"}),
        owner_id=owner.id,
        db=db_session,
    )

    assert not (subject_dir / "old_helper.py").exists()
    assert (subject_dir / "assignments" / "lab1" / "check.py").read_bytes() == b"v2"


async def test_duplicate_zip_with_missing_disk_dir_still_extracts(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """A hash match alone must not skip extraction forever: if a prior apply's disk
    swap never completed (e.g. crashed after the DB commit), the on-disk tree is
    missing despite the DB claiming success. Re-uploading the identical ZIP must
    self-heal by re-extracting, while still reporting 'unchanged' and NOT inserting
    a second SubjectPluginConfig row for the same content hash."""
    owner = await _make_owner(db_session)
    await db_session.commit()
    svc = ConfigApplyService(storage=None, plugins_dir=tmp_path)

    zip_bytes = _make_zip(_base_config(), extra_files={"assignments/lab1/check.py": b"code"})
    await svc.apply(zip_bytes, owner_id=owner.id, db=db_session)

    subject_dir = tmp_path / "demo101"
    assert subject_dir.is_dir()
    import shutil

    shutil.rmtree(subject_dir)
    assert not subject_dir.exists()

    result = await svc.apply(zip_bytes, owner_id=owner.id, db=db_session)

    assert result.changed is False
    assert result.subject_action == "unchanged"
    assert (subject_dir / "assignments" / "lab1" / "check.py").read_bytes() == b"code"

    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "demo101"))
    ).scalar_one()
    version_count = (
        await db_session.execute(
            select(func.count())
            .select_from(SubjectPluginConfig)
            .where(SubjectPluginConfig.subject_id == subject.id)
        )
    ).scalar_one()
    assert version_count == 1, "self-heal must not insert a duplicate SubjectPluginConfig row"


# ---------------------------------------------------------------------------
# 8. A new ZIP always reports as applied, whatever part of it changed
# ---------------------------------------------------------------------------


async def test_reapply_quiz_only_change_reports_updated(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """Quiz config lives only in the raw config blob, not in any DB column the
    field-level diff inspects. A re-upload whose only edit is the question bank
    must still be reported as an update — reporting 'unchanged' tells the teacher
    their edit was rejected when in fact a new version was stored."""
    owner = await _make_owner(db_session)
    await db_session.commit()
    svc = ConfigApplyService(storage=None, plugins_dir=tmp_path)

    cfg = _base_config()
    cfg["assignments"]["lab1"]["review_mode"] = "tests_then_quiz"
    cfg["assignments"]["lab1"]["quiz"] = {
        "pass_threshold_pct": 0.7,
        "questions": [{"type": "single_choice", "text": "Old question?", "points": 1}],
    }
    await svc.apply(_make_zip(cfg), owner_id=owner.id, db=db_session)

    cfg2 = _base_config()
    cfg2["assignments"]["lab1"]["review_mode"] = "tests_then_quiz"
    cfg2["assignments"]["lab1"]["quiz"] = {
        "pass_threshold_pct": 0.7,
        "questions": [{"type": "single_choice", "text": "New question?", "points": 1}],
    }
    result = await svc.apply(_make_zip(cfg2), owner_id=owner.id, db=db_session)

    assert result.changed is True
    assert result.subject_action == "updated"

    subject = (
        await db_session.execute(select(Subject).where(Subject.code == "demo101"))
    ).scalar_one()
    latest = (
        await db_session.execute(
            select(SubjectPluginConfig)
            .where(SubjectPluginConfig.subject_id == subject.id)
            .order_by(SubjectPluginConfig.version.desc())
            .limit(1)
        )
    ).scalar_one()
    assert latest.version == 2
    questions = latest.config["assignments"]["lab1"]["quiz"]["questions"]
    assert questions[0]["text"] == "New question?"


async def test_reapply_file_only_change_reports_updated(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """config.yml identical, checker script edited: the plugin tree on disk is
    replaced, so this is an update, not a no-op."""
    owner = await _make_owner(db_session)
    await db_session.commit()
    svc = ConfigApplyService(storage=None, plugins_dir=tmp_path)

    cfg = _base_config()
    await svc.apply(
        _make_zip(cfg, extra_files={"assignments/lab1/check.py": b"old"}),
        owner_id=owner.id,
        db=db_session,
    )
    result = await svc.apply(
        _make_zip(cfg, extra_files={"assignments/lab1/check.py": b"new"}),
        owner_id=owner.id,
        db=db_session,
    )

    assert result.subject_action == "updated"
    assert (tmp_path / "demo101" / "assignments" / "lab1" / "check.py").read_bytes() == b"new"


async def test_reapply_edited_content_file_is_reuploaded(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """A teacher who fixes a typo in TASK.md keeps the filename. The S3 object is
    keyed by that filename, so skipping the upload leaves students downloading the
    old document forever."""
    owner = await _make_owner(db_session)
    await db_session.commit()

    uploaded: list[tuple[str, bytes]] = []

    async def _capture(local_path: Path, s3_key: str) -> str:
        # The bytes must be read here: apply() deletes its temp tree on return.
        uploaded.append((s3_key, Path(local_path).read_bytes()))
        return f"https://cdn/{s3_key}"

    storage = AsyncMock()
    storage.upload_file = AsyncMock(side_effect=_capture)
    storage.delete_file = AsyncMock()
    svc = ConfigApplyService(storage=storage, plugins_dir=tmp_path)

    cfg = _base_config()
    cfg["assignments"]["lab1"]["contentFiles"] = [{"filename": "task.md", "displayName": "Task"}]
    await svc.apply(
        _make_zip(cfg, extra_files={"assignments/lab1/task.md": b"# Task v1"}),
        owner_id=owner.id,
        db=db_session,
    )
    uploaded.clear()

    await svc.apply(
        _make_zip(cfg, extra_files={"assignments/lab1/task.md": b"# Task v2, typo fixed"}),
        owner_id=owner.id,
        db=db_session,
    )

    assert uploaded == [("subjects/demo101/assignments/lab1/task.md", b"# Task v2, typo fixed")]
