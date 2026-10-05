"""Classroom ingest: roster -> student links, turned-in submissions -> versioned works.

Used by the nightly job and by the teacher's "Синхронізувати зараз" button. Every
submission is processed (and committed) on its own, so one bad file never aborts a sync.
A dead refresh token (`invalid_grant`) is the exception: it marks the connection and the
subject, commits, and propagates so the caller can move on.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from submissions_checker.core.logging import get_logger
from submissions_checker.db.models.classroom import (
    ClassroomStudentLink,
    ClassroomWork,
    LLMGrading,
)
from submissions_checker.db.models.enums import (
    ClassroomLinkMethod,
    GoogleConnectionStatus,
    LLMGradingStatus,
)
from submissions_checker.db.models.google_connection import GoogleConnection
from submissions_checker.db.models.group import Group
from submissions_checker.db.models.student import Student
from submissions_checker.db.models.subject import Subject, SubjectsStudents
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.services.google.client import (
    EXPORTS,
    DownloadedFile,
    StudentSubmissionRef,
)
from submissions_checker.services.google.matching import Candidate, RosterEntry, match
from submissions_checker.services.google.oauth import GoogleAuthError
from submissions_checker.services.llm_grading.config import is_llm_graded

logger = get_logger(__name__)

KEPT_STATES = frozenset({"TURNED_IN", "RETURNED"})
MAX_FILES_PER_WORK = 10

# Values of `subjects.classroom_sync_error` (rendered via vocab.classroom.sync_error_*).
SYNC_ERROR_RECONNECT = "reconnect"
SYNC_ERROR_GOOGLE = "google"
SYNC_ERROR_PARTIAL = "partial"

# Session-level advisory lock serialising Classroom ingest: the nightly job (either
# replica) and the teacher's "sync now" button never ingest at the same time.
ADVISORY_LOCK_KEY = 0x5C1A55


async def try_classroom_lock(conn: AsyncConnection) -> bool:
    """Take the Classroom lock on ``conn`` if free; it lives as long as the connection.

    Commits right away so the holder does not sit "idle in transaction" for the run.
    """
    got = bool(
        (
            await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
        ).scalar()
    )
    await conn.commit()
    return got


async def unlock_classroom(conn: AsyncConnection) -> None:
    await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
    await conn.commit()


@asynccontextmanager
async def classroom_lock(engine: AsyncEngine) -> AsyncIterator[bool]:
    """Yield whether the lock was taken, on a dedicated connection released on exit."""
    async with engine.connect() as conn:
        got = await try_classroom_lock(conn)
        try:
            yield got
        finally:
            if got:
                try:
                    await unlock_classroom(conn)
                except Exception:  # noqa: BLE001 — closing the session drops the lock too
                    logger.warning("classroom_unlock_failed", exc_info=True)
                    await conn.invalidate()


class ClassroomApi(Protocol):
    async def list_students(self, course_id: str) -> list[RosterEntry]: ...

    async def list_submissions(
        self, course_id: str, coursework_id: str
    ) -> list[StudentSubmissionRef]: ...

    async def file_meta(self, drive_id: str) -> dict[str, Any]: ...

    async def download(self, drive_id: str) -> DownloadedFile: ...


class ObjectStore(Protocol):
    async def upload_bytes(
        self, data: bytes, key: str, content_type: str = "application/octet-stream"
    ) -> str: ...


@dataclass
class IngestReport:
    roster: int = 0
    new_links: int = 0
    new_versions: int = 0
    unchanged: int = 0
    errors: int = 0


def storage_key(
    subject_id: int, assignment_id: int, submission_id: str, content_hash: str, name: str
) -> str:
    # Leading dots stripped too: MinIO rejects a ".." segment and it reads as traversal.
    safe = re.sub(r"[^\w.\-]+", "_", name).lstrip(".")[:120] or "file"
    return f"classroom/{subject_id}/{assignment_id}/{submission_id}/{content_hash[:12]}/{safe}"


async def _candidates(db: AsyncSession, subject_id: int) -> list[Candidate]:
    rows = (
        await db.execute(
            select(Student.id, Student.full_name, Student.email, Group.name)
            .join(SubjectsStudents, SubjectsStudents.student_id == Student.id)
            .join(Group, Group.id == Student.group_id)
            .where(SubjectsStudents.subject_id == subject_id)
            .order_by(Student.id)
        )
    ).all()
    return [Candidate(sid, name, email, group) for sid, name, email, group in rows]


async def release_waiting(db: AsyncSession, link: ClassroomStudentLink) -> None:
    """Flip this link's WAITING_LINK gradings to PENDING (caller owns the transaction)."""
    work_ids = select(ClassroomWork.id).where(ClassroomWork.link_id == link.id)
    await db.execute(
        update(LLMGrading)
        .where(
            LLMGrading.status == LLMGradingStatus.WAITING_LINK.value,
            LLMGrading.classroom_work_id.in_(work_ids),
        )
        .values(status=LLMGradingStatus.PENDING.value)
    )


async def upsert_links(db: AsyncSession, subject: Subject, roster: list[RosterEntry]) -> int:
    """Create links for unseen roster users and re-match unmatched ones.

    MANUAL/IGNORED/EMAIL/NAME rows keep their student; only the Classroom name and email
    are refreshed. Returns the number of rows created. Flushes, does not commit.
    """
    subject_id = subject.id
    existing = {
        link.classroom_user_id: link
        for link in (
            await db.execute(
                select(ClassroomStudentLink).where(ClassroomStudentLink.subject_id == subject_id)
            )
        )
        .scalars()
        .all()
    }
    candidates = await _candidates(db, subject_id)
    created = 0
    for entry in roster:
        if not entry.user_id:
            continue
        name = (entry.full_name or entry.email or entry.user_id)[:255]
        email = entry.email[:255] if entry.email else None
        link = existing.get(entry.user_id)
        if link is None:
            result = match(entry, candidates)
            link = ClassroomStudentLink(
                subject_id=subject_id,
                classroom_user_id=entry.user_id,
                classroom_email=email,
                classroom_name=name,
                student_id=result.student_id,
                method=result.method.value,
                score=result.score,
                candidates=result.candidates,
                confirmed=result.method == ClassroomLinkMethod.EMAIL,
            )
            db.add(link)
            existing[entry.user_id] = link
            created += 1
            continue
        link.classroom_email = email
        link.classroom_name = name
        if link.method != ClassroomLinkMethod.NONE.value or link.student_id is not None:
            continue
        result = match(entry, candidates)
        link.score = result.score
        link.candidates = result.candidates
        if result.method in (ClassroomLinkMethod.NAME, ClassroomLinkMethod.EMAIL):
            link.method = result.method.value
            link.student_id = result.student_id
            link.confirmed = result.method == ClassroomLinkMethod.EMAIL
            await db.flush()
            await release_waiting(db, link)
    await db.flush()
    return created


def _modified_set(manifest: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(str(e.get("drive_id")), str(e.get("modified"))) for e in manifest}


async def _ingest_submission(
    db: AsyncSession,
    subject_id: int,
    assignment_id: int,
    sub: StudentSubmissionRef,
    client: ClassroomApi,
    storage: ObjectStore,
    report: IngestReport,
) -> None:
    link = (
        await db.execute(
            select(ClassroomStudentLink).where(
                ClassroomStudentLink.subject_id == subject_id,
                ClassroomStudentLink.classroom_user_id == sub.user_id,
            )
        )
    ).scalar_one_or_none()
    if link is None:
        # The submitter is no longer on the roster: nothing to attach the work to.
        logger.info(
            "classroom_ingest_no_link",
            subject_id=subject_id,
            classroom_submission_id=sub.id,
        )
        return
    if link.method == ClassroomLinkMethod.IGNORED.value:
        return

    now = datetime.now(UTC)
    metas = [await client.file_meta(f.id) for f in sub.files]
    current = {
        (f.id, str(m.get("modifiedTime", ""))) for f, m in zip(sub.files, metas, strict=True)
    }

    latest = (
        await db.execute(
            select(ClassroomWork)
            .where(ClassroomWork.classroom_submission_id == sub.id)
            .order_by(ClassroomWork.seen_at.desc(), ClassroomWork.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is not None and _modified_set(latest.manifest) == current:
        latest.state, latest.late, latest.seen_at = sub.state, sub.late, now
        await db.commit()
        report.unchanged += 1
        return

    manifest: list[dict[str, Any]] = []
    contents: list[bytes | None] = []
    for i, (ref, meta) in enumerate(zip(sub.files, metas, strict=True)):
        entry: dict[str, Any] = {
            "drive_id": ref.id,
            "name": str(meta.get("name") or ref.title or ref.id),
            "mime": str(meta.get("mimeType", "")),
            "size": int(meta.get("size", 0) or 0),
            "modified": str(meta.get("modifiedTime", "")),
            "sha256": None,
            "storage_key": None,
            "skipped": None,
        }
        content: bytes | None = None
        if i >= MAX_FILES_PER_WORK:
            entry["skipped"] = "too_many"
        else:
            downloaded = await client.download(ref.id)
            entry["name"] = downloaded.name
            entry["mime"] = EXPORTS.get(downloaded.mime, downloaded.mime)
            if downloaded.content is None:
                entry["skipped"] = downloaded.skipped or "unavailable"
            else:
                content = downloaded.content
                entry["size"] = len(content)
                entry["sha256"] = hashlib.sha256(content).hexdigest()
        manifest.append(entry)
        contents.append(content)

    content_hash = hashlib.sha256(
        "".join(sorted(e["sha256"] for e in manifest if e["sha256"])).encode()
    ).hexdigest()

    same = (
        await db.execute(
            select(ClassroomWork).where(
                ClassroomWork.classroom_submission_id == sub.id,
                ClassroomWork.content_hash == content_hash,
            )
        )
    ).scalar_one_or_none()
    if same is not None:
        # Identical bytes under a new manifest (re-turn-in, touched file): keep the stored
        # objects, refresh the manifest so the next sync is a cheap skip again.
        stored = {e.get("sha256"): e.get("storage_key") for e in same.manifest}
        for entry in manifest:
            entry["storage_key"] = stored.get(entry["sha256"]) if entry["sha256"] else None
        same.manifest = manifest
        same.state, same.late, same.seen_at = sub.state, sub.late, now
        await db.commit()
        report.unchanged += 1
        return

    used: set[str] = set()
    for idx, (entry, content) in enumerate(zip(manifest, contents, strict=True)):
        if content is None:
            continue
        key = storage_key(subject_id, assignment_id, sub.id, content_hash, entry["name"])
        if key in used:
            key = storage_key(
                subject_id, assignment_id, sub.id, content_hash, f"{idx}_{entry['name']}"
            )
        used.add(key)
        await storage.upload_bytes(content, key, entry["mime"] or "application/octet-stream")
        entry["storage_key"] = key

    work = ClassroomWork(
        subjects_assignment_id=assignment_id,
        link_id=link.id,
        classroom_submission_id=sub.id,
        state=sub.state,
        late=sub.late,
        content_hash=content_hash,
        manifest=manifest,
        seen_at=now,
    )
    db.add(work)
    await db.flush()
    status = LLMGradingStatus.PENDING if link.student_id else LLMGradingStatus.WAITING_LINK
    db.add(LLMGrading(classroom_work_id=work.id, status=status.value))
    await db.commit()
    report.new_versions += 1
    logger.info(
        "classroom_work_ingested",
        subject_id=subject_id,
        classroom_submission_id=sub.id,
        work_id=work.id,
        grading_status=status.value,
    )


def _is_invalid_grant(exc: BaseException) -> bool:
    return isinstance(exc, GoogleAuthError) and exc.invalid_grant


async def _mark_reconnect(db: AsyncSession, subject_id: int, connection_id: int | None) -> None:
    await db.rollback()
    if connection_id is not None:
        conn = await db.get(GoogleConnection, connection_id)
        if conn is not None:
            conn.status = GoogleConnectionStatus.ERROR.value
            conn.last_error = "invalid_grant"
    subject = await db.get(Subject, subject_id)
    if subject is not None:
        subject.classroom_sync_error = SYNC_ERROR_RECONNECT
    await db.commit()


async def _set_sync_result(
    db: AsyncSession, subject_id: int, *, synced: bool, error: str | None
) -> None:
    subject = await db.get(Subject, subject_id)
    if subject is None:
        return
    if synced:
        subject.classroom_synced_at = datetime.now(UTC)
    subject.classroom_sync_error = error
    await db.commit()


async def ingest_subject(
    db: AsyncSession, subject: Subject, client: ClassroomApi, storage: ObjectStore
) -> IngestReport:
    """Sync one subject's roster and LLM-graded coursework submissions.

    Commits per submission. Raises GoogleAuthError (after marking the connection) when
    the refresh token is dead, and re-raises a roster failure after recording it.
    """
    # A rollback earlier in this session (e.g. the previous subject of the nightly loop)
    # expires the instance; reload it rather than lazy-load in async code.
    await db.refresh(subject)
    subject_id = subject.id
    course_id = subject.classroom_course_id
    connection_id = subject.classroom_connection_id
    report = IngestReport()
    if not course_id:
        return report

    try:
        roster = await client.list_students(course_id)
        report.roster = len(roster)
        report.new_links = await upsert_links(db, subject, roster)
        await db.commit()
    except Exception as exc:
        if _is_invalid_grant(exc):
            await _mark_reconnect(db, subject_id, connection_id)
        else:
            await db.rollback()
            await _set_sync_result(db, subject_id, synced=False, error=SYNC_ERROR_GOOGLE)
        raise

    assignments = [
        (a.id, a.classroom_coursework_id)
        for a in (
            await db.execute(
                select(SubjectsAssignment)
                .where(
                    SubjectsAssignment.subject_id == subject_id,
                    SubjectsAssignment.classroom_coursework_id.is_not(None),
                )
                .order_by(SubjectsAssignment.id)
            )
        )
        .scalars()
        .all()
        if is_llm_graded(a.config)
    ]

    for assignment_id, coursework_id in assignments:
        assert coursework_id is not None
        try:
            submissions = await client.list_submissions(course_id, coursework_id)
        except Exception as exc:
            if _is_invalid_grant(exc):
                await _mark_reconnect(db, subject_id, connection_id)
                raise
            # A deleted or inaccessible coursework must not block the other assignments.
            report.errors += 1
            logger.warning(
                "classroom_ingest_coursework_failed",
                subject_id=subject_id,
                coursework_id=coursework_id,
                error=str(exc),
            )
            continue

        for sub in submissions:
            if sub.state not in KEPT_STATES or not sub.files:
                continue
            try:
                await _ingest_submission(
                    db, subject_id, assignment_id, sub, client, storage, report
                )
            except Exception as exc:
                if _is_invalid_grant(exc):
                    await _mark_reconnect(db, subject_id, connection_id)
                    raise
                await db.rollback()
                report.errors += 1
                logger.exception(
                    "classroom_ingest_submission_failed",
                    subject_id=subject_id,
                    classroom_submission_id=sub.id,
                    error=str(exc),
                )

    await _set_sync_result(
        db, subject_id, synced=True, error=SYNC_ERROR_PARTIAL if report.errors else None
    )
    logger.info(
        "classroom_ingest_done",
        subject_id=subject_id,
        roster=report.roster,
        new_links=report.new_links,
        new_versions=report.new_versions,
        unchanged=report.unchanged,
        errors=report.errors,
    )
    return report
