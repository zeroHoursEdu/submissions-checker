"""settle(): set a student's grade and submission status from what is done.

Called after every event that can change a requirement — a quiz finishing or being
re-scored, checks or AI review finishing, a teacher saving the board or rejecting, a new
Classroom version, an extra attempt. The unit is the student's assignment (the whole squad
for a squad), not one submission: requirements are independent, so a new upload must not
wipe a passed quiz or saved scores. Idempotent; never commits.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core.logging import get_logger
from submissions_checker.core.state_machine import transition
from submissions_checker.db.models import (
    QuizAttempt,
    StudentAssignment,
    SubjectsAssignment,
    Submission,
)
from submissions_checker.db.models.enums import SubmissionSourceType, SubmissionStatus
from submissions_checker.db.models.squad import Squad
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.services import quiz_grants, requirements, squads
from submissions_checker.services.grading import squad_quiz_pct
from submissions_checker.workers.tasks.notification_tasks import (
    enqueue_teacher_review_notification,
)

logger = get_logger(__name__)

REJECTED_KEY = "rejected"
S = SubmissionStatus
SETTLED = frozenset({S.PENDING, S.QUIZ_SENT, S.AWAITING_TEACHER_REVIEW, S.COMPLETED, S.FAILED})
PROCESSING = frozenset({S.VALIDATING, S.TESTING, S.AWAITING_AI_REVIEW, S.AI_REVIEWING})
CHECK_FAILED = frozenset({S.VALIDATION_FAILED, S.TEST_FAILED, S.AI_REVIEW_FAILED})
_EVENT_TARGET = {
    "requirements_met": S.COMPLETED,
    "requirement_failed": S.FAILED,
    "awaiting_teacher": S.AWAITING_TEACHER_REVIEW,
    "awaiting_quiz": S.QUIZ_SENT,
}

Fact = tuple[requirements.State, float | None]


@dataclass(frozen=True)
class SettleResult:
    submission_id: int | None
    old_status: SubmissionStatus | None
    new_status: SubmissionStatus | None
    evaluation: requirements.Evaluation

    @property
    def completed_now(self) -> bool:
        return self.new_status == S.COMPLETED and self.old_status != S.COMPLETED


def plan_for(asg: SubjectsAssignment) -> requirements.Plan:
    return requirements.from_config(asg.config, span=asg.max_grade - asg.min_grade)


def _pct(score: float | None, max_score: float | None) -> float | None:
    if score is None or not max_score:
        return None
    return float(score) / float(max_score) * 100.0


async def _attempts(db: AsyncSession, sub_ids: list[int]) -> list[QuizAttempt]:
    if not sub_ids:
        return []
    rows = await db.execute(select(QuizAttempt).where(QuizAttempt.submission_id.in_(sub_ids)))
    return list(rows.scalars().all())


async def quiz_host(db: AsyncSession, subs: list[Submission]) -> Submission | None:
    """The submission the student's quiz attempts hang off: the latest that has any,
    else the latest one. Attempt counts and grants are per submission, so a later upload
    must not reset them."""
    attempts = await _attempts(db, [s.id for s in subs])
    with_attempts = {a.submission_id for a in attempts}
    return next((s for s in subs if s.id in with_attempts), subs[0] if subs else None)


async def _quiz_fact(db: AsyncSession, subs: list[Submission], squad: Squad | None) -> Fact:
    attempts = await _attempts(db, [s.id for s in subs])
    for sub in subs:
        if await squads.quiz_complete(db, sub):
            if squad is not None:
                pct, _ = squad_quiz_pct([a for a in attempts if a.submission_id == sub.id])
                return "done", pct
            best = max(
                (
                    p
                    for a in attempts
                    if a.is_passed and (p := _pct(a.score, a.max_score)) is not None
                ),
                default=None,
            )
            return "done", best
    host = await quiz_host(db, subs)
    if host is not None:
        base = await quiz_grants.base_max_attempts(db, host)
        if base is not None:
            for sid in await quiz_grants.students_on(db, host):
                # This student's rows plus legacy NULL rows, as the quiz gate counts them.
                mine = [
                    a
                    for a in attempts
                    if a.submission_id == host.id and a.student_id in (sid, None)
                ]
                cap = quiz_grants.effective_max_attempts(base, host, sid)
                if quiz_grants.is_exhausted(mine, cap):
                    return "failed", None
    return "pending", None


def _code_facts(subs: list[Submission]) -> tuple[Fact, Fact]:
    code = next((s for s in subs if s.source_type == SubmissionSourceType.ZIP_UPLOAD), None)
    if code is None:
        return ("pending", None), ("pending", None)
    st = code.status
    if st in (S.PENDING, S.VALIDATING, S.TESTING):
        return ("running", None), ("pending", None)
    if st in (S.VALIDATION_FAILED, S.TEST_FAILED):
        return ("failed", None), ("pending", None)
    tr = code.test_results or {}
    tests_pct = _pct(tr.get("score"), tr.get("max_score"))
    tests: Fact = ("done", tests_pct) if tests_pct is not None else ("pending", None)
    mark = (code.ai_review or {}).get("code_mark")
    ai: Fact
    if st in (S.AWAITING_AI_REVIEW, S.AI_REVIEWING):
        ai = ("running", None)
    elif st == S.AI_REVIEW_FAILED:
        ai = ("failed", None)
    elif isinstance(mark, int | float) and not isinstance(mark, bool):
        ai = ("done", float(mark))
    else:
        ai = ("pending", None)
    return tests, ai


async def _scope(db: AsyncSession, sa: StudentAssignment, asg: SubjectsAssignment) -> list[int]:
    return await squads.scope_sa_ids(db, sa.student_id, asg.subject_id, asg.id) or [sa.id]


async def scope_submissions(
    db: AsyncSession,
    sa: StudentAssignment,
    asg: SubjectsAssignment,
    *,
    scope: list[int] | None = None,
    fresh: bool = False,
) -> tuple[list[Submission], list[int]]:
    """(the scope's submissions newest first, the scope's SA ids). ``scope`` reuses ids the
    caller already resolved (and locked) instead of looking them up again."""
    if scope is None:
        scope = await _scope(db, sa, asg)
    subs = list(
        (
            await db.execute(
                select(Submission)
                .where(Submission.students_assignment_id.in_(scope))
                .order_by(Submission.created_at.desc(), Submission.id.desc())
                .execution_options(populate_existing=fresh)
            )
        )
        .scalars()
        .all()
    )
    return subs, scope


async def gather_facts(
    db: AsyncSession,
    sa: StudentAssignment,
    asg: SubjectsAssignment,
    *,
    fresh: bool = False,
    scope: list[int] | None = None,
) -> tuple[requirements.Facts, list[Submission], Squad | None, list[int]]:
    """(facts, scope submissions newest first, squad, scope SA ids).

    ``fresh`` reloads submissions already in the session from the database (settle uses it
    after taking its lock, having flushed first, so nothing pending is lost). ``scope``:
    the SA ids settle already locked, so the scope is not recomputed under the lock."""
    subs, scope = await scope_submissions(db, sa, asg, scope=scope, fresh=fresh)
    squad = await squads.active_squad(db, asg.subject_id, sa.student_id)
    tests, ai = _code_facts(subs)
    facts = requirements.Facts(
        quiz=await _quiz_fact(db, subs, squad),
        tests=tests,
        ai_review=ai,
        scores=dict(sa.teacher_scores or {}),
    )
    return facts, subs, squad, scope


async def _new_upload_less(
    db: AsyncSession, sa: StudentAssignment, asg: SubjectsAssignment, squad: Squad | None
) -> Submission:
    config_id = await db.scalar(
        select(SubjectPluginConfig.id)
        .where(SubjectPluginConfig.subject_id == asg.subject_id)
        .order_by(SubjectPluginConfig.version.desc())
        .limit(1)
    )
    sub = Submission(
        students_assignment_id=sa.id,
        source_type=SubmissionSourceType.QUIZ_ONLY,
        source_metadata={},
        status=S.PENDING,
        plugin_config_id=config_id,
        squad_id=squad.id if squad else None,
        test_results={"skipped": True, "reason": "requirements"},
    )
    db.add(sub)
    await db.flush()
    return sub


async def _write_grade(
    db: AsyncSession,
    latest: Submission,
    scope: list[int],
    squad: Squad | None,
    evaluation: requirements.Evaluation,
    subs: list[Submission],
) -> None:
    for sa_id in scope:
        row = await db.get(StudentAssignment, sa_id)
        if row is not None:
            row.grade = evaluation.grade
    breakdown = dict(evaluation.breakdown or {})
    if squad is not None:
        # Same attempts as the quiz percentage: the submission that completed the quiz.
        completing = [s.id for s in subs if await squads.quiz_complete(db, s)][:1]
        _, members = squad_quiz_pct(await _attempts(db, completing))
        breakdown["squad"] = {"unified": True, "squad_id": squad.id, "members": members}
    latest.grade_breakdown = breakdown
    # The sessions run with autoflush off; flush so a caller's later query or refresh in
    # the same transaction sees the grade.
    await db.flush()


async def lock_scope(db: AsyncSession, sa: StudentAssignment, asg: SubjectsAssignment) -> list[int]:
    """Row-lock every StudentAssignment in the scope (ordered by id, so squad members never
    deadlock each other) and reload them. Two members finishing their last attempts at once
    then settle one after the other, and the second reads the facts only after the first
    committed — under READ COMMITTED it sees the first one's pass.

    NO KEY UPDATE, not UPDATE: inserting a submission or quiz attempt takes KEY SHARE on the
    referenced row, which plain FOR UPDATE would wait on — two squad members could then each
    hold what the other needs. Returns the locked scope's SA ids.

    Public so a caller that writes the submission before settling (the attempt grant) can
    take this lock first and keep the order scope -> submission; settle re-locking rows its
    own transaction holds is a no-op. Flushes first: a pending submission write would
    otherwise be sent here, before the scope lock, inverting that order."""
    await db.flush()  # the reload below must not discard the caller's pending changes
    scope = await _scope(db, sa, asg)
    await db.execute(
        select(StudentAssignment)
        .where(StudentAssignment.id.in_(scope))
        .order_by(StudentAssignment.id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    return scope


def _waits_only_on_teacher(evaluation: requirements.Evaluation) -> bool:
    """Every required non-teacher item (quiz, tests, AI review) is done: what is left is
    the teacher's."""
    return not any(
        i.type not in requirements.TEACHER_TYPES
        and not i.optional
        and i.state in ("pending", "running")
        for i in evaluation.items
    )


async def settle(
    db: AsyncSession, sa: StudentAssignment, *, previous_status: SubmissionStatus | None = None
) -> SettleResult:
    """Grade and status of ``sa`` (and its squad) from what is done now. Does not commit.

    ``previous_status``: the status before the caller's own transition (a worker applies
    ``processing_done`` and then settles); logged only. The teacher is notified whenever
    the result waits on the teacher alone (idempotent per submission), so it no longer
    depends on which status the submission came from.
    """
    asg = await db.get(SubjectsAssignment, sa.subjects_assignment_id)
    if asg is None:
        raise ValueError(f"students_assignment {sa.id} has no assignment")
    plan = plan_for(asg)
    locked = await lock_scope(db, sa, asg)
    facts, subs, squad, scope = await gather_facts(db, sa, asg, fresh=True, scope=locked)
    evaluation = requirements.evaluate(
        plan, facts, asg.min_grade, asg.max_grade, round_up=squad is not None
    )
    if not subs:
        if not evaluation.complete:
            return SettleResult(None, None, None, evaluation)
        subs = [await _new_upload_less(db, sa, asg, squad)]
    latest = subs[0]
    # Settles are serialised by the scope lock; this guards against status writers outside
    # settle. NO KEY UPDATE, not UPDATE: a quiz attempt insert holds KEY SHARE on its
    # submission, and FOR UPDATE would wait for that member's transaction while it waits
    # for our scope lock — a deadlock.
    await db.refresh(
        latest, attribute_names=["status", "source_metadata"], with_for_update={"key_share": True}
    )
    old = latest.status
    if (
        old in PROCESSING
        or old in CHECK_FAILED
        or (old == S.PENDING and latest.source_type == SubmissionSourceType.ZIP_UPLOAD)
    ):
        return SettleResult(latest.id, old, old, evaluation)
    if old == S.COMPLETED:
        if evaluation.complete:
            await _write_grade(db, latest, scope, squad, evaluation, subs)
        else:
            logger.warning("settle_completed_incomplete", submission_id=latest.id)
        return SettleResult(latest.id, old, old, evaluation)

    if (latest.source_metadata or {}).get(REJECTED_KEY) or evaluation.failed:
        event = "requirement_failed"
    elif evaluation.complete:
        event = "requirements_met"
    elif evaluation.pending_teacher() or not plan.has("quiz"):
        event = "awaiting_teacher"
    else:
        event = "awaiting_quiz"
    if _EVENT_TARGET[event] != old:
        transition(latest, event)
    if event == "requirements_met":
        await _write_grade(db, latest, scope, squad, evaluation, subs)
    await db.flush()
    new = latest.status
    if new == S.AWAITING_TEACHER_REVIEW and _waits_only_on_teacher(evaluation):
        # Not on merely entering the status: while a quiz (or checks) is still open the work
        # is not the teacher's to look at yet. The enqueue is idempotent per (teacher,
        # submission) for good, so a later settle that finds the same wait adds nothing.
        await enqueue_teacher_review_notification(db, latest.id)
    logger.info(
        "settled",
        submission_id=latest.id,
        old=str(old),
        new=str(new),
        previous=str(previous_status) if previous_status is not None else None,
        grade=evaluation.grade,
    )
    return SettleResult(latest.id, old, new, evaluation)


async def reopen_after_new_version(
    db: AsyncSession, subjects_assignment_id: int, student_id: int
) -> None:
    """A new Classroom version of the work: a teacher's rejection of the old one no longer
    applies, so drop it and settle again. No-op for a student with no row for the
    assignment. Does not commit."""
    sa = await db.scalar(
        select(StudentAssignment).where(
            StudentAssignment.student_id == student_id,
            StudentAssignment.subjects_assignment_id == subjects_assignment_id,
        )
    )
    if sa is None:
        return
    asg = await db.get(SubjectsAssignment, subjects_assignment_id)
    if asg is None:
        return
    # Scope lock first (scope -> submission), as everywhere else.
    scope = await lock_scope(db, sa, asg)
    subs, _ = await scope_submissions(db, sa, asg, scope=scope, fresh=True)
    if subs and (subs[0].source_metadata or {}).get(REJECTED_KEY):
        subs[0].source_metadata = {
            k: v for k, v in (subs[0].source_metadata or {}).items() if k != REJECTED_KEY
        }
        await db.flush()
    await settle(db, sa)
