"""The quiz scoring rule, on its own.

Extracted from ``student_quiz._grade_and_finalize``, which also drives the submission
state machine, emits an outbox message and commits. Two callers need only the
arithmetic — the normal finalize path and the dispute regrade, which re-scores attempts
that closed long ago — so the rule lives here, pure and DB-free.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from submissions_checker.db.models.quiz_dispute import QuizQuestionOverride
from submissions_checker.db.models.quiz_template import QuizAnswer

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from submissions_checker.db.models.quiz_template import QuizAttempt

DEFAULT_PASS_THRESHOLD_PCT = 0.6


async def load_question_overrides(
    db: AsyncSession,
    plugin_config_id: int | None,
    plugin_config_version: int | None,
) -> set[int]:
    """Question ids a teacher has credited to everyone for this exact config version.

    Empty when the attempt is not pinned to a config version — there is then no scope in
    which "everyone who drew this question" is a well-defined set.
    """
    if plugin_config_id is None or plugin_config_version is None:
        return set()
    rows = await db.execute(
        select(QuizQuestionOverride.question_id).where(
            QuizQuestionOverride.plugin_config_id == plugin_config_id,
            QuizQuestionOverride.plugin_config_version == plugin_config_version,
            QuizQuestionOverride.credit_all.is_(True),
        )
    )
    return set(rows.scalars().all())


def apply_question_overrides(
    attempt: QuizAttempt,
    override_ids: set[int],
    db: Any,
) -> int:
    """Force every overridden question in this attempt's snapshot to full credit.

    Returns the number of answer rows written. Assignment, never accumulation, so calling
    this twice is the same as calling it once — an accepted ruling may be replayed.

    ``timed_out`` is left alone: the question really was lost to the clock, and the result
    page's distinction between "wrong" and "ran out of time" stays honest. Only the credit
    changes.

    A question the attempt reached but never answered gets a row created here. That is safe
    only for an attempt that is finished — a live attempt would later INSERT a second row
    for the same question (``answer_question`` never upserts), doubling the points. Live
    attempts are therefore not passed through this function; they pick the override up from
    ``load_question_overrides`` when they finalize.
    """
    if not override_ids:
        return 0

    by_question = {a.question_id: a for a in attempt.answers}
    changed = 0

    for q_snap in attempt.questions_snapshot or []:
        q_id = q_snap.get("id")
        if q_id not in override_ids:
            continue
        points = int(q_snap.get("points", 0))
        existing = by_question.get(q_id)
        if existing is not None:
            existing.is_correct = True
            existing.points_earned = points
        else:
            created = QuizAnswer(
                attempt_id=attempt.id,
                question_id=q_id,
                answer={},
                is_correct=True,
                points_earned=points,
                timed_out=False,
            )
            db.add(created)
            attempt.answers.append(created)
        changed += 1

    return changed


def score_attempt(attempt: QuizAttempt) -> tuple[int, int, bool, bool]:
    """Score an attempt from its answers and its frozen question snapshot.

    Returns ``(score, max_score, is_passed, force_failed)``. Mutates nothing.

    ``max_score`` comes from the snapshot rather than from the answers, so questions the
    student never reached still count against them.

    An anti-cheat ``_force_fail`` flag never passes, but the score is still computed:
    the teacher reviewing the violation wants to see what the attempt was worth.
    """
    force_failed = bool((attempt.violations or {}).get("_force_fail", False))

    score = sum(a.points_earned or 0 for a in attempt.answers)
    max_score = sum(q["points"] for q in attempt.questions_snapshot)

    if force_failed:
        return score, max_score, False, True

    threshold = attempt.config_snapshot.get("pass_threshold_pct", DEFAULT_PASS_THRESHOLD_PCT)
    is_passed = (score / max_score) >= threshold if max_score > 0 else False
    return score, max_score, is_passed, False
