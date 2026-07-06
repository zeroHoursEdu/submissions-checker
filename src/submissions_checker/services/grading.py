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

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from submissions_checker.core.logging import get_logger
from submissions_checker.db.models import StudentAssignment, SubjectsAssignment, Submission

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
) -> GradeBreakdown:
    """Compute the final grade from 0–100 component scores.

    ``works_pct`` / ``ai_mark`` / ``quiz_pct`` are each 0–100 or None (absent →
    weight dropped). The blended 0–100 grade is scaled into ``[min_grade,
    max_grade]`` and clamped to it.
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
    grade = max(min_grade, min(max_grade, round(scaled)))

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


async def finalize_grade(db: AsyncSession, submission: Submission) -> GradeBreakdown | None:
    """Compute and persist the final grade for a completed submission.

    Reads the works score from ``test_results``, the AI mark from ``ai_review``,
    and the quiz score from the submission's passed quiz attempt (if any); writes
    ``StudentAssignment.grade`` and ``submission.grade_breakdown``. Returns the
    breakdown, or None if the owning assignment could not be resolved.
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

    breakdown = compute_grade(
        grading_cfg,
        subjects_assignment.min_grade,
        subjects_assignment.max_grade,
        works_pct=works_pct,
        ai_mark=ai_mark,
        quiz_pct=quiz_pct,
    )

    sa.grade = breakdown.grade
    sub.grade_breakdown = breakdown.to_dict()
    logger.info(
        "finalize_grade",
        submission_id=submission.id,
        grade=breakdown.grade,
        works=works_pct,
        quality=ai_mark,
        quiz=quiz_pct,
    )
    return breakdown
