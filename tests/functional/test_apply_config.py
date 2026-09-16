"""Functional tests for POST /teacher/subjects/apply-config.

This is the ONLY path to create or update a Subject (ZIP upload ->
ConfigApplyService.apply). These tests drive the real FastAPI app over ASGI
through the functional harness (full auth/authz stack, real Postgres), exercising
the handler in ``teacher_portal.apply_subject_config``.

Handler behaviour (see api/routes/teacher_portal.py ~L112-139) that these tests
pin down:

* Auth/authz comes from the ``TeacherUser`` dependency: anonymous -> 401,
  student -> 403, teacher/admin allowed.
* On success the handler issues a 303 redirect to
  ``/teacher?apply_result={subject_action}`` (created / updated / unchanged).
* The handler catches PermissionError, ValueError AND the generic Exception and
  maps every one of them to a 303 redirect to ``/teacher?apply_error=...``.
  i.e. NO service exception ever escapes as a 500; the only non-303 statuses on
  this route come from the auth dependency. We assert that mapping explicitly.

The valid-ZIP layout (config.yml schema) mirrors tests/integration/test_config_apply.py.
"""

from __future__ import annotations

import io
import urllib.parse
import zipfile
from typing import Any

import pytest
import yaml
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.db.models.enums import SubjectStatus
from submissions_checker.db.models.subject import Subject
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.db.models.user import User

pytestmark = pytest.mark.asyncio

ENDPOINT = "/teacher/subjects/apply-config"


# ── Helpers ──────────────────────────────────────────────────────────────────


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


def _make_zip(config: dict[str, Any], extra_files: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("config.yml", yaml.safe_dump(config))
        for rel_path, data in (extra_files or {}).items():
            zf.writestr(rel_path, data)
    return buf.getvalue()


def _upload(zip_bytes: bytes, filename: str = "config.zip") -> dict[str, Any]:
    """Build the multipart 'files' payload for the ``config_zip`` UploadFile."""
    return {"config_zip": (filename, io.BytesIO(zip_bytes), "application/zip")}


async def _post(client: AsyncClient, zip_bytes: bytes, filename: str = "config.zip"):
    # follow_redirects=False so we can assert the 303 + Location directly.
    return await client.post(ENDPOINT, files=_upload(zip_bytes, filename), follow_redirects=False)


def _redirect_query(resp) -> dict[str, list[str]]:
    location = resp.headers["location"]
    return urllib.parse.parse_qs(urllib.parse.urlparse(location).query)


async def _count_subjects(db: AsyncSession, code: str = "demo101") -> int:
    return (
        await db.execute(select(func.count()).select_from(Subject).where(Subject.code == code))
    ).scalar_one()


# ── 1. Permissions ───────────────────────────────────────────────────────────


async def test_anonymous_is_rejected(client: AsyncClient, db: AsyncSession) -> None:
    resp = await _post(client, _make_zip(_base_config()))
    assert resp.status_code == 401
    assert await _count_subjects(db) == 0


async def test_student_is_forbidden(student_client: AsyncClient, db: AsyncSession) -> None:
    resp = await _post(student_client, _make_zip(_base_config()))
    assert resp.status_code == 403
    assert await _count_subjects(db) == 0


async def test_teacher_is_allowed(teacher_client: AsyncClient, db: AsyncSession) -> None:
    resp = await _post(teacher_client, _make_zip(_base_config()))
    assert resp.status_code == 303
    assert await _count_subjects(db) == 1


async def test_admin_is_allowed(admin_client: AsyncClient, db: AsyncSession) -> None:
    resp = await _post(admin_client, _make_zip(_base_config()))
    assert resp.status_code == 303
    assert await _count_subjects(db) == 1


# ── 2. Happy path: create, then upsert ───────────────────────────────────────


async def test_teacher_create_subject_full_side_effects(
    teacher_client: AsyncClient, teacher: User, db: AsyncSession
) -> None:
    resp = await _post(teacher_client, _make_zip(_base_config()))

    assert resp.status_code == 303
    assert _redirect_query(resp)["apply_result"] == ["created"]
    assert urllib.parse.urlparse(resp.headers["location"]).path == "/teacher"

    subject = (await db.execute(select(Subject).where(Subject.code == "demo101"))).scalar_one()
    assert subject.name == "Demo 101"
    assert subject.description == "A demo subject"
    assert subject.owner_id == teacher.id  # acting teacher becomes the owner
    assert subject.status == SubjectStatus.ACTIVE

    assignments = (
        (
            await db.execute(
                select(SubjectsAssignment).where(SubjectsAssignment.subject_id == subject.id)
            )
        )
        .scalars()
        .all()
    )
    assert [a.code for a in assignments] == ["lab1"]
    assert assignments[0].title == "Lab 1"

    cfg = (
        await db.execute(
            select(SubjectPluginConfig).where(SubjectPluginConfig.subject_id == subject.id)
        )
    ).scalar_one()
    assert cfg.version == 1
    assert cfg.config["subjectCode"] == "demo101"


async def test_reupload_by_same_owner_upserts_new_version(
    teacher_client: AsyncClient, teacher: User, db: AsyncSession
) -> None:
    create = await _post(teacher_client, _make_zip(_base_config()))
    assert _redirect_query(create)["apply_result"] == ["created"]

    subject_id = (
        await db.execute(select(Subject.id).where(Subject.code == "demo101"))
    ).scalar_one()

    # Updated config: rename subject + add lab2.
    cfg2 = _base_config()
    cfg2["name"] = "Demo 101 (v2)"
    cfg2["assignments"]["lab2"] = {
        "title": "Lab 2",
        "min_grade": 10,
        "max_grade": 80,
        "review_mode": "tests_then_teacher",
    }

    update = await _post(teacher_client, _make_zip(cfg2))
    assert update.status_code == 303
    assert _redirect_query(update)["apply_result"] == ["updated"]

    # Same subject row (upsert by code), not a new one.
    assert await _count_subjects(db) == 1
    subject = (await db.execute(select(Subject).where(Subject.code == "demo101"))).scalar_one()
    assert subject.id == subject_id
    assert subject.name == "Demo 101 (v2)"

    assignment_codes = sorted(
        (
            await db.execute(
                select(SubjectsAssignment.code).where(SubjectsAssignment.subject_id == subject_id)
            )
        )
        .scalars()
        .all()
    )
    assert assignment_codes == ["lab1", "lab2"]

    versions = (
        (
            await db.execute(
                select(SubjectPluginConfig.version)
                .where(SubjectPluginConfig.subject_id == subject_id)
                .order_by(SubjectPluginConfig.version)
            )
        )
        .scalars()
        .all()
    )
    assert versions == [1, 2]


async def test_reupload_identical_zip_is_unchanged(
    teacher_client: AsyncClient, db: AsyncSession
) -> None:
    zip_bytes = _make_zip(_base_config())
    await _post(teacher_client, zip_bytes)
    again = await _post(teacher_client, zip_bytes)

    assert again.status_code == 303
    assert _redirect_query(again)["apply_result"] == ["unchanged"]

    subject_id = (
        await db.execute(select(Subject.id).where(Subject.code == "demo101"))
    ).scalar_one()
    version_count = (
        await db.execute(
            select(func.count())
            .select_from(SubjectPluginConfig)
            .where(SubjectPluginConfig.subject_id == subject_id)
        )
    ).scalar_one()
    assert version_count == 1  # dedup by content hash, no new version


# ── 3. Ownership on update ───────────────────────────────────────────────────


async def test_non_owner_reupload_is_mapped_to_apply_error_redirect(
    client: AsyncClient,
    make_user,
    db: AsyncSession,
) -> None:
    owner = await make_user(username="owner_a")
    intruder = await make_user(username="owner_b")

    # Owner creates the subject.
    from tests.functional.conftest import authenticate

    authenticate(client, owner)
    create = await _post(client, _make_zip(_base_config()))
    assert _redirect_query(create)["apply_result"] == ["created"]

    # A DIFFERENT teacher tries to re-apply a config with the same code.
    client.cookies.clear()
    authenticate(client, intruder)
    cfg2 = _base_config()
    cfg2["name"] = "Hijacked"
    resp = await _post(client, _make_zip(cfg2))

    # config_apply raises PermissionError for a non-owner re-apply; the handler
    # catches it and maps it to a 303 redirect with apply_error (NOT a 500/403).
    # NOTE: the handler does not surface PermissionError as HTTP 403 — it folds
    # it into the same 303 ?apply_error redirect used for validation failures.
    assert resp.status_code == 303
    query = _redirect_query(resp)
    assert "apply_error" in query
    assert "owned by another teacher" in query["apply_error"][0]

    # Owner + name unchanged; no second subject created.
    assert await _count_subjects(db) == 1
    subject = (await db.execute(select(Subject).where(Subject.code == "demo101"))).scalar_one()
    assert subject.owner_id == owner.id
    assert subject.name == "Demo 101"


# ── 4. Invalid uploads ───────────────────────────────────────────────────────


async def test_non_zip_upload_is_rejected(teacher_client: AsyncClient, db: AsyncSession) -> None:
    resp = await _post(teacher_client, b"this is plainly not a zip", filename="config.zip")

    assert resp.status_code == 303
    query = _redirect_query(resp)
    assert "apply_error" in query
    assert "not a valid ZIP" in query["apply_error"][0]
    assert await _count_subjects(db) == 0


async def test_zip_missing_config_yml_is_rejected(
    teacher_client: AsyncClient, db: AsyncSession
) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "no config here")

    resp = await _post(teacher_client, buf.getvalue())

    assert resp.status_code == 303
    query = _redirect_query(resp)
    assert "apply_error" in query
    assert "must contain config.yml" in query["apply_error"][0]
    assert await _count_subjects(db) == 0


async def test_empty_subject_code_is_rejected(
    teacher_client: AsyncClient, db: AsyncSession
) -> None:
    cfg = _base_config()
    cfg["subjectCode"] = ""
    resp = await _post(teacher_client, _make_zip(cfg))

    assert resp.status_code == 303
    query = _redirect_query(resp)
    assert "apply_error" in query
    assert "subjectCode" in query["apply_error"][0]
    assert await _count_subjects(db) == 0


async def test_malformed_config_yml_is_rejected(
    teacher_client: AsyncClient, db: AsyncSession
) -> None:
    # config.yml parses to a bare scalar string, so ``new_cfg.get(...)`` raises
    # AttributeError inside apply(). That is neither ValueError nor PermissionError,
    # so it falls through to the handler's generic ``except Exception`` arm, which
    # still returns a 303 redirect with a generic apply_error message (no 500).
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("config.yml", "just a bare string, not a mapping")

    resp = await _post(teacher_client, buf.getvalue())

    assert resp.status_code == 303
    query = _redirect_query(resp)
    assert "apply_error" in query
    # Generic catch-all message, not the field-specific one.
    assert "unexpected error" in query["apply_error"][0].lower()
    assert await _count_subjects(db) == 0


async def test_oversize_zip_is_rejected(teacher_client: AsyncClient, db: AsyncSession) -> None:
    # Just over the 50 MB limit enforced in ConfigApplyService.apply.
    big = b"\x00" * (50 * 1024 * 1024 + 1)
    resp = await _post(teacher_client, big)

    assert resp.status_code == 303
    query = _redirect_query(resp)
    assert "apply_error" in query
    assert "50 MB limit" in query["apply_error"][0]
    assert await _count_subjects(db) == 0


async def test_short_answer_question_is_rejected(
    teacher_client: AsyncClient, db: AsyncSession
) -> None:
    cfg = _base_config()
    cfg["assignments"]["lab1"]["review_mode"] = "tests_then_quiz"
    cfg["assignments"]["lab1"]["quiz"] = {
        "questions": [
            {
                "type": "single_choice",
                "text": "ok?",
                "choices": [{"text": "y", "is_correct": True}],
            },
            {"type": "short_answer", "text": "explain"},
        ]
    }
    resp = await _post(teacher_client, _make_zip(cfg))
    assert resp.status_code == 303
    location = urllib.parse.unquote(resp.headers["location"])
    assert "apply_error=" in location
    assert "short_answer" in location
    assert (await db.execute(select(func.count()).select_from(Subject))).scalar_one() == 0


async def test_quiz_under_tests_only_is_rejected_on_upload(
    teacher_client: AsyncClient, db: AsyncSession
) -> None:
    cfg = _base_config()
    cfg["assignments"]["lab1"]["quiz"] = {
        "questions": [{"type": "true_false", "text": "?", "correct": True}]
    }
    resp = await _post(teacher_client, _make_zip(cfg))
    assert resp.status_code == 303
    assert "never sends it" in urllib.parse.unquote(resp.headers["location"])
    assert (await db.execute(select(func.count()).select_from(Subject))).scalar_one() == 0
