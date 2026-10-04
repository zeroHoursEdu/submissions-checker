"""Weighted final-grade calculation.

The final grade blends a *code score* and a *quiz score* by configurable
weights; the code score itself blends a *works score* (fraction of test points
earned) and a *quality score* (the AI code mark). Any component with no data is
dropped and the remaining weights are renormalized, so a partial or slightly
misconfigured ``grading`` block still yields a sane grade rather than an error.

``compute_grade`` is a pure function (unit-testable, DB-free). ``finalize_grade``
is the persistence wrapper called at every submission-completion point.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from submissions_checker.core.logging import get_logger
from submissions_checker.db.models import StudentAssignment, SubjectsAssignment, Submission
from submissions_checker.services import teacher_scores

logger = get_logger(__name__)


@dataclass(frozen=True)
class GradeBreakdown:
    """Component scores (0–100) and the effective weights that produced a grade."""

    grade: int
    works_score: float | None
    quality_score: float | None
    quiz_score: float | None
    code_weight: float
    quiz_weight: float
    works_weight: float
    quality_weight: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _blend(pairs: list[tuple[float | None, float]]) -> float | None:
    """Weighted mean of ``(value, weight)`` pairs, ignoring value=None.

    Weights of present components are renormalized so they sum to 1. Returns None
    if no component has a value.
    """
    present = [(v, w) for v, w in pairs if v is not None and w > 0]
    total_weight = sum(w for _, w in present)
    if not present or total_weight <= 0:
        return None
    return sum(v * w for v, w in present) / total_weight


def compute_grade(
    grading_cfg: dict[str, Any] | None,
    min_grade: int,
    max_grade: int,
    *,
    works_pct: float | None,
    ai_mark: float | None,
    quiz_pct: float | None,
    round_up: bool = False,
) -> GradeBreakdown:
    """Compute the final grade from 0–100 component scores.

    ``works_pct`` / ``ai_mark`` / ``quiz_pct`` are each 0–100 or None (absent →
    weight dropped). The blended 0–100 grade is scaled into ``[min_grade,
    max_grade]`` and clamped to it. When ``round_up`` is True, rounds to the
    higher grade (ceil) instead of the nearest grade (round).
    """
    cfg = grading_cfg or {}
    code_cfg = cfg.get("code") or {}
    code_weight = float(cfg.get("code_weight", 1.0))
    quiz_weight = float(cfg.get("quiz_weight", 0.0))
    works_weight = float(code_cfg.get("works_weight", 1.0))
    quality_weight = float(code_cfg.get("quality_weight", 0.0))

    code_score = _blend([(works_pct, works_weight), (ai_mark, quality_weight)])
    blended = _blend([(code_score, code_weight), (quiz_pct, quiz_weight)])

    # No component at all → floor to min_grade (matches "nothing earned").
    normalized = blended if blended is not None else 0.0
    scaled = min_grade + (max_grade - min_grade) * (normalized / 100.0)
    # Squads round up ("average, rounded to the higher mark"); round(…, 6) first so an
    # exact 6.0 that floats as 6.0000000001 does not ceil to 7.
    rounded = math.ceil(round(scaled, 6)) if round_up else round(scaled)
    grade = max(min_grade, min(max_grade, rounded))

    return GradeBreakdown(
        grade=grade,
        works_score=works_pct,
        quality_score=ai_mark,
        quiz_score=quiz_pct,
        code_weight=code_weight,
        quiz_weight=quiz_weight,
        works_weight=works_weight,
        quality_weight=quality_weight,
    )


def _pct(score: float | int | None, max_score: float | int | None) -> float | None:
    if not max_score or max_score <= 0 or score is None:
        return None
    return (score / max_score) * 100.0


def squad_quiz_pct(attempts: Iterable[Any]) -> tuple[float | None, list[dict[str, Any]]]:
    """Mean quiz percentage over squad members, one passed attempt per ``student_id``.

    Returns ``(mean_pct, members)`` where ``members`` is the per-member list stored in
    ``grade_breakdown["squad"]["members"]``. ``(None, [])`` when nobody has passed yet.
    """
    best: dict[int, float] = {}
    for a in attempts:
        if not a.is_passed or a.student_id is None:
            continue
        pct = _pct(a.score, a.max_score)
        if pct is not None and pct > best.get(a.student_id, -1.0):
            best[a.student_id] = pct
    if not best:
        return None, []
    members = [{"student_id": sid, "quiz_pct": round(p, 2)} for sid, p in sorted(best.items())]
    return sum(best.values()) / len(best), members


async def finalize_grade(db: AsyncSession, submission: Submission) -> dict[str, Any] | None:
    """Compute and persist the final grade for a completed submission.

    Reads the works score from ``test_results``, the AI mark from ``ai_review``,
    and the quiz score from the submission's passed quiz attempt (if any) — or, under
    ``quiz_and_teacher_scores``, the quiz plus the teacher's points; writes
    ``StudentAssignment.grade`` and ``submission.grade_breakdown``. Returns the stored
    breakdown, or None if the owning assignment could not be resolved or (scored mode)
    a half of the grade is still missing.
    """
    result = await db.execute(
        select(Submission)
        .where(Submission.id == submission.id)
        .options(
            selectinload(Submission.students_assignment).selectinload(
                StudentAssignment.subjects_assignment
            ),
            selectinload(Submission.quiz_attempts),
        )
    )
    sub = result.scalar_one_or_none() or submission
    sa: StudentAssignment | None = sub.students_assignment
    subjects_assignment: SubjectsAssignment | None = sa.subjects_assignment if sa else None
    if sa is None or subjects_assignment is None:
        logger.warning("finalize_grade_no_assignment", submission_id=submission.id)
        return None

    grading_cfg = (subjects_assignment.config or {}).get("grading")

    test_results = sub.test_results or {}
    works_pct = _pct(test_results.get("score"), test_results.get("max_score"))

    ai_review = sub.ai_review or {}
    raw_mark = ai_review.get("code_mark")
    ai_mark = float(raw_mark) if isinstance(raw_mark, int | float) else None

    passed_attempt = next((a for a in sub.quiz_attempts if a.is_passed), None)
    quiz_pct = _pct(passed_attempt.score, passed_attempt.max_score) if passed_attempt else None

    # Local import: squads imports nothing from grading, but importing it at module
    # scope would still be fine — kept local to mirror the brief's seam.
    from submissions_checker.services import squads

    squad = await squads.squad_for_submission(db, sub)
    squad_members: list[dict[str, Any]] = []
    if squad is not None:
        quiz_pct, squad_members = squad_quiz_pct(sub.quiz_attempts)

    if teacher_scores.is_scored_mode(subjects_assignment.config):
        # quiz_and_teacher_scores: the quiz is a gate and half of the grade; the teacher's
        # points are the other half. Both must be in, or there is no grade yet.
        crits = teacher_scores.criteria(grading_cfg)
        scores = sa.teacher_scores or {}
        if quiz_pct is None or not teacher_scores.is_complete(crits, scores):
            logger.info("finalize_grade_scored_incomplete", submission_id=submission.id)
            return None
        breakdown_dict = teacher_scores.compute(
            grading_cfg,
            subjects_assignment.min_grade,
            subjects_assignment.max_grade,
            quiz_pct=quiz_pct,
            scores=scores,
            round_up=squad is not None,
        )
        grade = int(breakdown_dict["grade"])
    else:
        breakdown = compute_grade(
            grading_cfg,
            subjects_assignment.min_grade,
            subjects_assignment.max_grade,
            works_pct=works_pct,
            ai_mark=ai_mark,
            quiz_pct=quiz_pct,
            round_up=squad is not None,
        )
        grade = breakdown.grade
        breakdown_dict = breakdown.to_dict()

    target_sa_ids = [sa.id]
    if squad is not None:
        target_sa_ids = await squads.member_sa_ids(db, squad, subjects_assignment.id)
    for sa_id in target_sa_ids:
        row = await db.get(StudentAssignment, sa_id)
        if row is not None:
            row.grade = grade
    if squad is not None:
        breakdown_dict["squad"] = {"unified": True, "squad_id": squad.id, "members": squad_members}
    sub.grade_breakdown = breakdown_dict
    logger.info(
        "finalize_grade",
        submission_id=submission.id,
        grade=grade,
        works=works_pct,
        quality=ai_mark,
        quiz=quiz_pct,
    )
    return breakdown_dict
