"""LLM grading runner: grade one work, reap crashed jobs, run the nightly grading loop.

Each job commits on its own (RUNNING before the judge call, DONE/FAILED after it), so a
crash mid-run loses at most one job, which the next run's ``reap_stale`` turns FAILED.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from submissions_checker.core.logging import get_logger
from submissions_checker.core.metrics import llm_gradings_total
from submissions_checker.db.models.classroom import ClassroomWork, LLMGrading
from submissions_checker.db.models.enums import LLMGradingStatus
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.services.llm_grading.config import llm_criteria
from submissions_checker.services.llm_grading.judge import (
    GradingRequest,
    JudgeCriterion,
    JudgeError,
    LLMJudge,
    WorkFile,
)

logger = get_logger(__name__)

MAX_ATTEMPTS = 3
STALE_AFTER = timedelta(hours=1)
NO_GRADABLE_FILES = "no_gradable_files"
MAX_ERROR_CHARS = 2000
# Consecutive JudgeErrors after which the judge is presumed down (sidecar dead, quota gone)
# and the rest of tonight's work is left untouched instead of burning attempts.
JUDGE_ERROR_STREAK = 3

Outcome = Literal["done", "failed", "judge_error", "no_files"]


class BlobReader(Protocol):
    async def download_bytes(self, key: str) -> bytes: ...


def _gradable(manifest: list[Any]) -> list[dict[str, Any]]:
    return [e for e in manifest if not e.get("skipped") and e.get("storage_key")]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def night_start_for(start: datetime, tz: ZoneInfo) -> datetime:
    """Local midnight (in ``tz``) of the night ``start`` belongs to, as aware UTC.

    Both firings of a DST fall-back night share it, so they share one budget.
    """
    local_day = start.astimezone(tz).date()
    return datetime.combine(local_day, datetime.min.time(), tzinfo=tz).astimezone(UTC)


def _touch(grading: LLMGrading, now: datetime) -> None:
    """Stamp ``updated_at`` from our clock even when unchanged (else ``onupdate`` wins)."""
    grading.updated_at = now
    flag_modified(grading, "updated_at")


def _eligible() -> Any:
    return or_(
        LLMGrading.status == LLMGradingStatus.PENDING.value,
        and_(
            LLMGrading.status == LLMGradingStatus.FAILED.value,
            LLMGrading.attempts < MAX_ATTEMPTS,
        ),
    )


async def _work(db: AsyncSession, grading: LLMGrading) -> ClassroomWork:
    work = await db.get(ClassroomWork, grading.classroom_work_id)
    if work is None:  # FK with ON DELETE CASCADE: only a concurrent delete gets here
        raise RuntimeError(f"classroom work {grading.classroom_work_id} not found")
    return work


async def build_request(
    db: AsyncSession, grading: LLMGrading, storage: BlobReader
) -> GradingRequest:
    """The judge request for this grading: assignment config + the work's stored files."""
    work = await _work(db, grading)
    assignment = await db.get(SubjectsAssignment, work.subjects_assignment_id)
    if assignment is None:
        raise RuntimeError(f"assignment {work.subjects_assignment_id} not found")
    cfg = assignment.config or {}
    block = cfg.get("llm_grading") or {}
    criteria = [
        JudgeCriterion(c.key, c.title, c.max, c.requirements)
        for c in llm_criteria(cfg.get("grading"))
    ]
    files = [
        WorkFile(
            name=str(e["name"]),
            content=await storage.download_bytes(str(e["storage_key"])),
            mime=str(e.get("mime") or "application/octet-stream"),
        )
        for e in _gradable(work.manifest)
    ]
    return GradingRequest(
        task=str(block.get("task") or ""),
        instructions=str(block.get("instructions") or ""),
        criteria=criteria,
        files=files,
    )


async def grade_one(
    db: AsyncSession,
    grading: LLMGrading,
    judge: LLMJudge,
    storage: BlobReader,
    *,
    now_fn: Callable[[], datetime] = _utcnow,
) -> Outcome:
    """Run the judge on one grading and record DONE (with the draft) or FAILED.

    ``updated_at``/``graded_at`` are stamped from ``now_fn`` so the nightly budget, which
    counts tonight's finished jobs by these columns, uses the same clock as the loop.
    """
    grading.status = LLMGradingStatus.RUNNING.value
    _touch(grading, now_fn())
    await db.commit()
    work = await _work(db, grading)
    log = logger.bind(
        grading_id=grading.id, work_id=work.id, assignment_id=work.subjects_assignment_id
    )

    if not _gradable(work.manifest):
        # Nothing the judge could read: do not spend quota, and do not retry next night.
        grading.status = LLMGradingStatus.FAILED.value
        grading.error = NO_GRADABLE_FILES
        grading.attempts = MAX_ATTEMPTS
        _touch(grading, now_fn())
        await db.commit()
        llm_gradings_total.labels(outcome="no_files").inc()
        log.warning("llm_grading_no_gradable_files")
        return "no_files"

    log.info("llm_grading_started", attempt=grading.attempts + 1)
    started = time.monotonic()
    try:
        req = await build_request(db, grading, storage)
        result = await judge.grade(req)
    except Exception as exc:  # noqa: BLE001 — any failure is retried next night, bounded
        await db.rollback()
        await db.refresh(grading)
        grading.status = LLMGradingStatus.FAILED.value
        grading.error = (str(exc) or type(exc).__name__)[:MAX_ERROR_CHARS]
        grading.attempts += 1
        _touch(grading, now_fn())
        await db.commit()
        llm_gradings_total.labels(outcome="failed").inc()
        log.warning(
            "llm_grading_failed",
            attempts=grading.attempts,
            duration_s=round(time.monotonic() - started, 1),
            error=grading.error,
        )
        return "judge_error" if isinstance(exc, JudgeError) else "failed"

    now = now_fn()
    grading.status = LLMGradingStatus.DONE.value
    grading.draft = {
        "criteria": {
            key: {
                "points": v.points,
                "justification": v.justification,
                "evidence": v.evidence,
            }
            for key, v in result.criteria.items()
        },
        "comment": result.comment,
        "usage": result.usage,
    }
    grading.provider = result.provider
    grading.model = result.model
    grading.graded_at = now
    _touch(grading, now)
    grading.error = None
    await db.commit()
    llm_gradings_total.labels(outcome="done").inc()
    log.info(
        "llm_grading_done",
        provider=result.provider,
        model=result.model,
        files=len(req.files),
        duration_s=round(time.monotonic() - started, 1),
        usage=result.usage,
    )
    return "done"


async def reap_stale(db: AsyncSession, now: datetime) -> int:
    """RUNNING rows untouched for an hour were left by a crash: count them as a failed try."""
    result = await db.execute(
        update(LLMGrading)
        .where(
            LLMGrading.status == LLMGradingStatus.RUNNING.value,
            LLMGrading.updated_at < now - STALE_AFTER,
        )
        .values(
            status=LLMGradingStatus.FAILED.value,
            error="stale",
            attempts=LLMGrading.attempts + 1,
            updated_at=now,
        )
        .returning(LLMGrading.id)
    )
    reaped = list(result.scalars().all())
    await db.commit()
    if reaped:
        llm_gradings_total.labels(outcome="stale").inc(len(reaped))
        logger.warning("llm_grading_reaped_stale", grading_ids=reaped)
    return len(reaped)


async def _used_tonight(db: AsyncSession, night_start: datetime) -> int:
    """Jobs already finished this night (any earlier run, e.g. a DST double firing)."""
    count = await db.scalar(
        select(func.count())
        .select_from(LLMGrading)
        .where(
            or_(
                and_(
                    LLMGrading.status == LLMGradingStatus.DONE.value,
                    LLMGrading.graded_at >= night_start,
                ),
                and_(
                    LLMGrading.status == LLMGradingStatus.FAILED.value,
                    LLMGrading.updated_at >= night_start,
                ),
            )
        )
    )
    return int(count or 0)


def _candidate(night_start: datetime) -> Any:
    """Eligible, and not something that already failed tonight (no same-night retry)."""
    return and_(
        _eligible(),
        or_(
            LLMGrading.status != LLMGradingStatus.FAILED.value,
            LLMGrading.updated_at < night_start,
        ),
    )


async def grading_loop(
    db_factory: async_sessionmaker[AsyncSession],
    judge: LLMJudge,
    storage: BlobReader,
    *,
    now_fn: Callable[[], datetime],
    deadline: datetime,
    cap: int,
    night_start: datetime,
) -> int:
    """Grade eligible works, grouped by assignment, until ``deadline`` or tonight's budget.

    The budget is ``cap`` per night (from ``night_start``), shared by every run of that
    night. The work list is taken once up front. Returns the number of jobs run.
    """
    async with db_factory() as db:
        budget = cap - await _used_tonight(db, night_start)
        if budget <= 0:
            logger.info("llm_grading_budget_spent", cap=cap)
            return 0
        ids = list(
            (
                await db.execute(
                    select(LLMGrading.id)
                    .join(ClassroomWork, ClassroomWork.id == LLMGrading.classroom_work_id)
                    .where(_candidate(night_start))
                    .order_by(ClassroomWork.subjects_assignment_id, LLMGrading.id)
                    .limit(budget)
                )
            )
            .scalars()
            .all()
        )

    done = 0
    judge_errors = 0
    for grading_id in ids:
        if now_fn() >= deadline:
            logger.info("llm_grading_window_closed", graded=done, left=len(ids) - done)
            break
        if judge_errors >= JUDGE_ERROR_STREAK:
            logger.warning("llm_grading_circuit_open", graded=done, left=len(ids) - done)
            break
        try:
            async with db_factory() as db:
                grading = (
                    await db.execute(
                        select(LLMGrading).where(LLMGrading.id == grading_id, _eligible())
                    )
                ).scalar_one_or_none()
                if grading is None:  # approved, retried or reaped since the list was taken
                    continue
                outcome = await grade_one(db, grading, judge, storage, now_fn=now_fn)
                done += 1
                judge_errors = judge_errors + 1 if outcome == "judge_error" else 0
        except Exception:  # noqa: BLE001 — a DB hiccup on one job must not end the night
            logger.exception("llm_grading_job_error", grading_id=grading_id)
    logger.info("llm_grading_loop_done", graded=done, candidates=len(ids))
    return done
