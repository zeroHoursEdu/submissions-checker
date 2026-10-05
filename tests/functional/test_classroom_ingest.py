"""Classroom ingest: roster -> links, submissions -> versioned works + LLM gradings."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient
from sqlalchemy import select

from submissions_checker.api.routes import teacher_classroom
from submissions_checker.core.config import Settings, get_settings
from submissions_checker.db.models import Subject, SubjectsAssignment
from submissions_checker.db.models.audit_log import AuditLog
from submissions_checker.db.models.classroom import (
    ClassroomStudentLink,
    ClassroomWork,
    LLMGrading,
)
from submissions_checker.db.models.enums import UserRole
from submissions_checker.db.models.google_connection import GoogleConnection
from submissions_checker.db.models.subject import SubjectsStudents
from submissions_checker.main import app
from submissions_checker.services.google.client import (
    DownloadedFile,
    DriveFileRef,
    GoogleApiError,
    StudentSubmissionRef,
)
from submissions_checker.services.google import ingest as ingest_mod
from submissions_checker.services.google.crypto import encrypt_token
from submissions_checker.services.google.ingest import (
    IngestReport,
    ingest_subject,
    storage_key,
)
from submissions_checker.services.google.matching import RosterEntry
from submissions_checker.services.google.oauth import GoogleAuthError

pytestmark = pytest.mark.asyncio

KEY = Fernet.generate_key().decode()
LLM_CFG = {"review_mode": "quiz_and_teacher_scores", "llm_grading": {"enabled": True}}
T0 = "2026-10-01T10:00:00.000Z"
T1 = "2026-10-02T10:00:00.000Z"


def _settings() -> Settings:
    return Settings(
        secret_key="test-secret-key-minimum-32-chars-long",
        google_client_id="cid",
        google_client_secret="csecret",
        google_token_encryption_key=KEY,
    )


class FakeClient:
    """Duck-typed ClassroomClient over in-memory roster/submissions/files."""

    def __init__(self) -> None:
        self.roster: list[RosterEntry] = []
        self.submissions: dict[str, list[StudentSubmissionRef]] = {}
        # drive_id -> {"name", "mime", "modified", "content"}
        self.files: dict[str, dict[str, Any]] = {}
        self.fail_download: set[str] = set()
        self.roster_error: Exception | None = None
        self.downloads: list[str] = []
        self.events: list[str] = []

    async def list_courses(self) -> list[dict[str, str]]:
        return []

    async def list_coursework(self, course_id: str) -> list[dict[str, str]]:
        return [{"id": "w1", "title": "Lab in Classroom"}]

    async def list_students(self, course_id: str) -> list[RosterEntry]:
        if self.roster_error is not None:
            raise self.roster_error
        return list(self.roster)

    async def list_submissions(
        self, course_id: str, coursework_id: str
    ) -> list[StudentSubmissionRef]:
        return list(self.submissions.get(coursework_id, []))

    async def file_meta(self, drive_id: str) -> dict[str, Any]:
        f = self.files[drive_id]
        return {
            "id": drive_id,
            "name": f["name"],
            "mimeType": f["mime"],
            "size": None if f.get("no_size") else str(len(f["content"])),
            "modifiedTime": f["modified"],
        }

    async def download(self, drive_id: str) -> DownloadedFile:
        self.downloads.append(drive_id)
        self.events.append(f"download:{drive_id}")
        if drive_id in self.fail_download:
            raise GoogleApiError("boom", 500)
        f = self.files[drive_id]
        return DownloadedFile(drive_id, f["name"], f["mime"], f["modified"], f["content"], None)

    def add_file(self, drive_id: str, content: bytes, *, modified: str = T0) -> DriveFileRef:
        self.files[drive_id] = {
            "name": f"{drive_id}.pdf",
            "mime": "application/pdf",
            "modified": modified,
            "content": content,
        }
        return DriveFileRef(id=drive_id, title=f"{drive_id}.pdf")

    def submit(
        self, sub_id: str, user_id: str, files: list[DriveFileRef], *, state: str = "TURNED_IN"
    ) -> None:
        self.submissions.setdefault("w1", []).append(
            StudentSubmissionRef(id=sub_id, user_id=user_id, state=state, late=False, files=files)
        )


class FakeStorage:
    def __init__(self, events: list[str] | None = None) -> None:
        self.objects: dict[str, bytes] = {}
        self.events = events if events is not None else []

    async def upload_bytes(
        self, data: bytes, key: str, content_type: str = "application/octet-stream"
    ) -> str:
        self.events.append(f"upload:{data.decode(errors='replace')[:20]}")
        self.objects[key] = data
        return f"s3://{key}"


@pytest.fixture
async def setup(db, teacher, make_student, make_group):
    conn = GoogleConnection(
        user_id=teacher.id,
        google_email="t@edu.kpi.ua",
        refresh_token_enc=encrypt_token(_settings(), "rt"),
    )
    subject = Subject(name="S", owner_id=teacher.id)
    db.add_all([conn, subject])
    await db.commit()
    subject.classroom_course_id = "c1"
    subject.classroom_connection_id = conn.id
    asg = SubjectsAssignment(
        subject_id=subject.id, title="Lab", code="lab", config=LLM_CFG, max_grade=10
    )
    other = SubjectsAssignment(subject_id=subject.id, title="Quiz", code="quiz", config={})
    db.add_all([asg, other])
    await db.commit()
    asg.classroom_coursework_id = "w1"
    other.classroom_coursework_id = "w2"
    group = await make_group("ІП-43")
    komin = await make_student(group=group, email="komin@edu.kpi.ua", full_name="Комін Іван")
    rep = await make_student(group=group, email="rep@example.com", full_name="Репетуха Микита")
    db.add_all(
        [
            SubjectsStudents(subject_id=subject.id, student_id=komin.id),
            SubjectsStudents(subject_id=subject.id, student_id=rep.id),
        ]
    )
    await db.commit()
    client = FakeClient()
    client.roster = [
        RosterEntry("u1", "ІП-43 Komin Ivan", "komin@edu.kpi.ua"),
        RosterEntry("u2", "ІП-43 Repetukha Mykyta Olehovych", "mykyta.r@gmail.com"),
        RosterEntry("u3", "ІП-43 Shevchenko Taras", "taras@gmail.com"),
    ]
    return {
        "subject_id": subject.id,
        "conn_id": conn.id,
        "asg_id": asg.id,
        "komin_id": komin.id,
        "rep_id": rep.id,
        "subject": subject,
        "asg": asg,
        "conn": conn,
        "client": client,
        "storage": FakeStorage(),
        "komin": komin,
        "rep": rep,
        "group": group,
    }


async def _links(db, subject_id) -> dict[str, ClassroomStudentLink]:
    db.expire_all()
    rows = (
        (
            await db.execute(
                select(ClassroomStudentLink).where(ClassroomStudentLink.subject_id == subject_id)
            )
        )
        .scalars()
        .all()
    )
    return {r.classroom_user_id: r for r in rows}


async def _works(db) -> list[tuple[ClassroomWork, LLMGrading]]:
    db.expire_all()
    return list(
        (
            await db.execute(
                select(ClassroomWork, LLMGrading)
                .join(LLMGrading, LLMGrading.classroom_work_id == ClassroomWork.id)
                .order_by(ClassroomWork.id)
            )
        )
        .tuples()
        .all()
    )


def _three_submissions(client: FakeClient) -> None:
    client.submit("s1", "u1", [client.add_file("d1", b"komin work")])
    client.submit("s2", "u2", [client.add_file("d2", b"rep work")])
    client.submit("s3", "u3", [client.add_file("d3", b"taras work")])


async def test_first_sync_creates_links_works_and_pending_gradings(db, setup):
    s = setup
    _three_submissions(s["client"])
    report = await ingest_subject(db, s["subject"], s["client"], s["storage"])

    assert report == IngestReport(roster=3, new_links=3, new_versions=3, unchanged=0, errors=0)
    links = await _links(db, s["subject_id"])
    assert (links["u1"].method, links["u1"].confirmed, links["u1"].student_id) == (
        "EMAIL",
        True,
        s["komin_id"],
    )
    assert (links["u2"].method, links["u2"].confirmed, links["u2"].student_id) == (
        "NAME",
        False,
        s["rep_id"],
    )
    assert links["u2"].candidates and links["u2"].score >= 0.85
    assert (links["u3"].method, links["u3"].student_id) == ("NONE", None)

    works = await _works(db)
    by_sub = {w.classroom_submission_id: (w, g) for w, g in works}
    assert by_sub["s1"][1].status == "PENDING"
    assert by_sub["s2"][1].status == "PENDING"
    assert by_sub["s3"][1].status == "WAITING_LINK"
    w1 = by_sub["s1"][0]
    assert w1.state == "TURNED_IN" and w1.subjects_assignment_id == s["asg_id"]
    entry = w1.manifest[0]
    assert entry["drive_id"] == "d1" and entry["modified"] == T0
    assert entry["skipped"] is None and len(entry["sha256"]) == 64
    assert entry["storage_key"] in s["storage"].objects
    assert len(s["storage"].objects) == 3
    assert all(
        k.startswith(f"classroom/{s['subject_id']}/{s['asg_id']}/") for k in s["storage"].objects
    )

    await db.refresh(s["subject"])
    assert s["subject"].classroom_synced_at is not None
    assert s["subject"].classroom_sync_error is None


async def test_skips_new_and_created_states_and_no_attachments(db, setup):
    s = setup
    c = s["client"]
    c.submit("s1", "u1", [c.add_file("d1", b"x")], state="NEW")
    c.submit("s2", "u2", [c.add_file("d2", b"y")], state="CREATED")
    c.submit("s3", "u3", [], state="TURNED_IN")
    c.submit("s4", "u1", [c.add_file("d4", b"z")], state="RETURNED")
    report = await ingest_subject(db, s["subject"], c, s["storage"])
    assert report.new_versions == 1
    assert [w.classroom_submission_id for w, _ in await _works(db)] == ["s4"]
    assert c.downloads == ["d4"]


async def test_unchanged_manifest_is_not_reingested(db, setup):
    s = setup
    _three_submissions(s["client"])
    await ingest_subject(db, s["subject"], s["client"], s["storage"])
    s["client"].downloads.clear()

    report = await ingest_subject(db, s["subject"], s["client"], s["storage"])
    assert report.new_versions == 0 and report.unchanged == 3 and report.new_links == 0
    assert s["client"].downloads == []
    assert len(await _works(db)) == 3


async def test_same_hash_new_manifest_updates_only_state(db, setup):
    s = setup
    c = s["client"]
    c.submit("s1", "u1", [c.add_file("d1", b"same bytes")])
    await ingest_subject(db, s["subject"], c, s["storage"])

    c.files["d1"]["modified"] = T1
    c.submissions["w1"] = [
        StudentSubmissionRef(
            id="s1", user_id="u1", state="RETURNED", late=True, files=[DriveFileRef("d1", "")]
        )
    ]
    report = await ingest_subject(db, s["subject"], c, s["storage"])
    assert report.new_versions == 0 and report.unchanged == 1
    works = await _works(db)
    assert len(works) == 1
    work = works[0][0]
    assert (work.state, work.late) == ("RETURNED", True)
    assert work.manifest[0]["modified"] == T1

    # The refreshed manifest makes the next sync a cheap skip again.
    c.downloads.clear()
    await ingest_subject(db, s["subject"], c, s["storage"])
    assert c.downloads == []


async def test_changed_content_creates_new_version(db, setup):
    s = setup
    c = s["client"]
    c.submit("s1", "u1", [c.add_file("d1", b"v1")])
    await ingest_subject(db, s["subject"], c, s["storage"])
    first = (await _works(db))[0][1]
    first.status = "DONE"
    await db.commit()

    c.files["d1"].update(content=b"v2", modified=T1)
    report = await ingest_subject(db, s["subject"], c, s["storage"])
    assert report.new_versions == 1
    works = await _works(db)
    assert len(works) == 2
    assert works[0][0].content_hash != works[1][0].content_hash
    assert [g.status for _, g in works] == ["DONE", "PENDING"]
    assert len(s["storage"].objects) == 2


@pytest.mark.parametrize("old_status", ["PENDING", "WAITING_LINK", "FAILED"])
async def test_new_version_supersedes_ungraded_older_versions(db, setup, old_status):
    s = setup
    c = s["client"]
    c.submit("s1", "u1", [c.add_file("d1", b"v1")])
    await ingest_subject(db, s["subject"], c, s["storage"])
    first = (await _works(db))[0][1]
    first.status = old_status
    await db.commit()

    c.files["d1"].update(content=b"v2", modified=T1)
    await ingest_subject(db, s["subject"], c, s["storage"])
    assert [g.status for _, g in await _works(db)] == ["SUPERSEDED", "PENDING"]


async def test_new_version_keeps_running_and_done_older_gradings(db, setup):
    s = setup
    c = s["client"]
    c.submit("s1", "u1", [c.add_file("d1", b"v1")])
    c.submit("s2", "u1", [c.add_file("d2", b"w1")])
    await ingest_subject(db, s["subject"], c, s["storage"])
    by_sub = {w.classroom_submission_id: g for w, g in await _works(db)}
    by_sub["s1"].status = "DONE"
    by_sub["s2"].status = "RUNNING"
    await db.commit()

    c.files["d1"].update(content=b"v2", modified=T1)
    c.files["d2"].update(content=b"w2", modified=T1)
    await ingest_subject(db, s["subject"], c, s["storage"])
    rows = sorted((w.classroom_submission_id, w.id, g.status) for w, g in await _works(db))
    assert [r[2] for r in rows] == ["DONE", "PENDING", "RUNNING", "PENDING"]


async def test_reverting_to_an_older_version_revives_its_grading(db, setup):
    s = setup
    c = s["client"]
    c.submit("s1", "u1", [c.add_file("d1", b"v1")])
    await ingest_subject(db, s["subject"], c, s["storage"])
    c.files["d1"].update(content=b"v2", modified=T1)
    await ingest_subject(db, s["subject"], c, s["storage"])
    # The student puts the first file back: v1 is the latest again and must be graded.
    c.files["d1"].update(content=b"v1", modified="2026-10-03T10:00:00.000Z")
    await ingest_subject(db, s["subject"], c, s["storage"])
    works = await _works(db)
    assert len(works) == 2
    latest = max(works, key=lambda wg: (wg[0].seen_at, wg[0].id))
    assert latest[0].content_hash == works[0][0].content_hash
    assert [g.status for _, g in works] == ["PENDING", "SUPERSEDED"]


async def test_too_many_files_are_recorded_as_skipped(db, setup):
    s = setup
    c = s["client"]
    files = [c.add_file(f"d{i}", f"part {i}".encode()) for i in range(12)]
    c.submit("s1", "u1", files)
    await ingest_subject(db, s["subject"], c, s["storage"])
    manifest = (await _works(db))[0][0].manifest
    assert len(manifest) == 12
    assert [e["skipped"] for e in manifest].count("too_many") == 2
    assert len(c.downloads) == 10 and len(s["storage"].objects) == 10


async def test_work_byte_budget_skips_later_files_as_too_large(db, setup, monkeypatch):
    monkeypatch.setattr(ingest_mod, "MAX_WORK_BYTES", 25)
    s = setup
    c = s["client"]
    files = [
        c.add_file("d1", b"a" * 10),
        c.add_file("d2", b"b" * 10),
        c.add_file("d3", b"c" * 10),  # 30 > 25: over budget, never fetched
        c.add_file("d4", b"d" * 5),  # still fits after d3 is skipped
    ]
    c.submit("s1", "u1", files)
    await ingest_subject(db, s["subject"], c, s["storage"])
    manifest = (await _works(db))[0][0].manifest
    assert [e["skipped"] for e in manifest] == [None, None, "too_large", None]
    assert manifest[2]["storage_key"] is None and manifest[2]["sha256"] is None
    assert c.downloads == ["d1", "d2", "d4"]
    assert len(s["storage"].objects) == 3


async def test_budget_applies_to_exports_without_a_known_size(db, setup, monkeypatch):
    # Google Docs report no size in their metadata: the downloaded length decides.
    monkeypatch.setattr(ingest_mod, "MAX_WORK_BYTES", 15)
    s = setup
    c = s["client"]
    files = [c.add_file("d1", b"a" * 10), c.add_file("d2", b"b" * 10)]
    c.files["d2"]["no_size"] = True
    c.submit("s1", "u1", files)
    await ingest_subject(db, s["subject"], c, s["storage"])
    manifest = (await _works(db))[0][0].manifest
    assert [e["skipped"] for e in manifest] == [None, "too_large"]
    assert manifest[1]["storage_key"] is None
    assert len(s["storage"].objects) == 1


async def test_each_file_is_uploaded_before_the_next_download(db, setup):
    s = setup
    c = s["client"]
    s["storage"].events = c.events  # one shared timeline
    c.submit("s1", "u1", [c.add_file("d1", b"one"), c.add_file("d2", b"two")])
    await ingest_subject(db, s["subject"], c, s["storage"])
    assert c.events == ["download:d1", "upload:one", "download:d2", "upload:two"]
    work = (await _works(db))[0][0]
    expected = hashlib.sha256(
        "".join(sorted(hashlib.sha256(b).hexdigest() for b in (b"one", b"two"))).encode()
    ).hexdigest()
    assert work.content_hash == expected
    for entry in work.manifest:
        assert entry["storage_key"] == storage_key(
            s["subject_id"], s["asg_id"], "s1", entry["sha256"], entry["name"]
        )


async def test_ignored_link_not_downloaded(db, setup):
    s = setup
    c = s["client"]
    db.add(
        ClassroomStudentLink(
            subject_id=s["subject_id"],
            classroom_user_id="u3",
            classroom_name="x",
            method="IGNORED",
            confirmed=True,
        )
    )
    await db.commit()
    _three_submissions(c)
    report = await ingest_subject(db, s["subject"], c, s["storage"])
    assert report.new_versions == 2
    assert "d3" not in c.downloads
    links = await _links(db, s["subject_id"])
    assert links["u3"].method == "IGNORED"
    assert links["u3"].classroom_name == "ІП-43 Shevchenko Taras"


async def test_late_student_enrolment_matches_on_next_sync(db, setup, make_student):
    s = setup
    _three_submissions(s["client"])
    await ingest_subject(db, s["subject"], s["client"], s["storage"])

    taras = await make_student(group=s["group"], email="t@x.com", full_name="Шевченко Тарас")
    taras_id = taras.id
    db.add(SubjectsStudents(subject_id=s["subject_id"], student_id=taras_id))
    await db.commit()

    await ingest_subject(db, s["subject"], s["client"], s["storage"])
    links = await _links(db, s["subject_id"])
    assert (links["u3"].method, links["u3"].student_id) == ("NAME", taras_id)
    statuses = {w.classroom_submission_id: g.status for w, g in await _works(db)}
    assert statuses["s3"] == "PENDING"


async def test_manual_link_is_never_rematched(db, setup):
    s = setup
    db.add(
        ClassroomStudentLink(
            subject_id=s["subject_id"],
            classroom_user_id="u1",
            classroom_name="old",
            student_id=s["rep_id"],
            method="MANUAL",
            confirmed=True,
        )
    )
    await db.commit()
    await ingest_subject(db, s["subject"], s["client"], s["storage"])
    links = await _links(db, s["subject_id"])
    assert (links["u1"].method, links["u1"].student_id) == ("MANUAL", s["rep_id"])
    assert links["u1"].classroom_name == "ІП-43 Komin Ivan"


async def test_one_bad_file_does_not_abort_sync(db, setup):
    s = setup
    _three_submissions(s["client"])
    s["client"].fail_download.add("d2")
    report = await ingest_subject(db, s["subject"], s["client"], s["storage"])
    assert report.errors == 1 and report.new_versions == 2
    assert sorted(w.classroom_submission_id for w, _ in await _works(db)) == ["s1", "s3"]
    await db.refresh(s["subject"])
    assert s["subject"].classroom_synced_at is not None
    assert s["subject"].classroom_sync_error == "partial"


async def test_invalid_grant_marks_connection_error_and_continues(db, setup):
    s = setup
    s["client"].roster_error = GoogleAuthError("dead", invalid_grant=True)
    with pytest.raises(GoogleAuthError):
        await ingest_subject(db, s["subject"], s["client"], s["storage"])
    db.expire_all()
    conn = await db.get(GoogleConnection, s["conn_id"])
    subject = await db.get(Subject, s["subject_id"])
    assert conn is not None and subject is not None
    assert (conn.status, conn.last_error) == ("ERROR", "invalid_grant")
    assert subject.classroom_sync_error == "reconnect"


async def test_invalid_grant_during_download_also_marks_connection(db, setup):
    s = setup
    c = s["client"]
    _three_submissions(c)

    async def _dead(drive_id: str) -> DownloadedFile:
        raise GoogleAuthError("dead", invalid_grant=True)

    c.download = _dead  # type: ignore[method-assign]
    with pytest.raises(GoogleAuthError):
        await ingest_subject(db, s["subject"], c, s["storage"])
    db.expire_all()
    conn = await db.get(GoogleConnection, s["conn_id"])
    assert conn is not None and conn.status == "ERROR"


async def test_storage_key_sanitised():
    key = storage_key(1, 2, "s", "a" * 64, "../../x y.pdf")
    # Keyed by the file's own sha256, so it can be uploaded before the work hash exists.
    assert key.startswith("classroom/1/2/s/files/" + "a" * 16 + "_")
    assert "/" not in key.split("/")[-1]
    assert storage_key(1, 2, "s", "a" * 64, "").endswith("_file")
    # A bare ".." segment would be rejected by MinIO (and reads as traversal).
    assert storage_key(1, 2, "s", "a" * 64, "..").endswith("_file")


# ── Route ────────────────────────────────────────────────────────────────────


@pytest.fixture
def route_env(monkeypatch, setup):
    app.dependency_overrides[get_settings] = _settings
    monkeypatch.setattr(teacher_classroom, "_client", lambda *a: setup["client"])
    monkeypatch.setattr(teacher_classroom, "get_storage", lambda _s: setup["storage"])
    yield setup
    app.dependency_overrides.pop(get_settings, None)


async def test_sync_route_requires_subject_access(client: AsyncClient, login, make_user, route_env):
    other = await make_user(role=UserRole.TEACHER, username="other")
    login(client, other)
    resp = await client.post(f"/teacher/subjects/{route_env['subject_id']}/classroom/sync")
    assert resp.status_code == 403
    assert route_env["client"].downloads == []


async def test_sync_route_ingests_and_redirects(teacher_client: AsyncClient, db, route_env):
    _three_submissions(route_env["client"])
    sid = route_env["subject_id"]
    resp = await teacher_client.post(f"/teacher/subjects/{sid}/classroom/sync")
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/teacher/subjects/{sid}?classroom=synced&synced=3"
    assert len(await _works(db)) == 3
    audit = (
        await db.execute(select(AuditLog).where(AuditLog.action == "classroom_synced"))
    ).scalar_one()
    assert audit.target_id == sid

    page = await teacher_client.get(resp.headers["location"])
    assert "Синхронізацію завершено. Нових робіт: 3." in page.text
    assert f'action="/teacher/subjects/{sid}/classroom/sync"' in page.text


async def test_sync_route_not_linked(teacher_client: AsyncClient, db, route_env):
    subject = route_env["subject"]
    subject.classroom_course_id = None
    await db.commit()
    resp = await teacher_client.post(f"/teacher/subjects/{subject.id}/classroom/sync")
    assert resp.headers["location"].endswith("classroom_error=not_linked")


async def test_sync_route_inactive_connection(teacher_client: AsyncClient, db, route_env):
    route_env["conn"].status = "ERROR"
    await db.commit()
    resp = await teacher_client.post(f"/teacher/subjects/{route_env['subject_id']}/classroom/sync")
    assert resp.headers["location"].endswith("classroom_error=not_linked")


async def test_sync_route_without_storage(teacher_client: AsyncClient, monkeypatch, route_env):
    monkeypatch.setattr(teacher_classroom, "get_storage", lambda _s: None)
    resp = await teacher_client.post(f"/teacher/subjects/{route_env['subject_id']}/classroom/sync")
    assert resp.headers["location"].endswith("classroom_error=storage")


async def test_sync_route_invalid_grant_flashes_reconnect(
    teacher_client: AsyncClient, db, route_env
):
    route_env["client"].roster_error = GoogleAuthError("dead", invalid_grant=True)
    sid = route_env["subject_id"]
    resp = await teacher_client.post(f"/teacher/subjects/{sid}/classroom/sync")
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("classroom_error=reconnect")
    page = await teacher_client.get(resp.headers["location"])
    assert "Google відкликав доступ. Підключіть Google знову." in page.text
    db.expire_all()
    conn = await db.get(GoogleConnection, route_env["conn_id"])
    assert conn is not None and conn.status == "ERROR"


async def test_sync_route_google_failure_flashes(teacher_client: AsyncClient, route_env):
    route_env["client"].roster_error = GoogleApiError("down", 503)
    resp = await teacher_client.post(f"/teacher/subjects/{route_env['subject_id']}/classroom/sync")
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("classroom_error=google")
