"""One-off backfill for the move from review modes to a ``requirements:`` list.

    python -m submissions_checker.cli.backfill_requirements --subject 1          # dry run
    python -m submissions_checker.cli.backfill_requirements --subject 1 --apply

Work the teacher accepted under the old «Затвердити» is COMPLETED; under the new model that
acceptance is a ``check`` item, so it is ticked (=1) on every scope member's teacher_scores.
Every SA row of the subject that has a submission is then settled, and no grade may change:
any change aborts without writing. A dry run (the default) always rolls back.

Settling stuck submissions may enqueue teacher review notifications (outbox); with --apply
those stay, in a dry run they are rolled back with everything else.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.db.models import StudentAssignment, SubjectsAssignment
from submissions_checker.db.models.enums import SubmissionStatus
from submissions_checker.db.session import get_session
from submissions_checker.services import settle as settle_service
from submissions_checker.services import squads


@dataclass
class Report:
    ticked: int = 0
    settled: int = 0
    status_changes: list[tuple[int, str, str]] = field(default_factory=list)
    grade_changes: list[tuple[int, int | None, int | None]] = field(default_factory=list)


class _Rollback(Exception):
    """Raised inside the savepoint to undo the whole run."""


async def _grades(db: AsyncSession, subject_id: int) -> dict[int, int | None]:
    rows = await db.execute(
        select(StudentAssignment.id, StudentAssignment.grade)
        .join(SubjectsAssignment, SubjectsAssignment.id == StudentAssignment.subjects_assignment_id)
        .where(SubjectsAssignment.subject_id == subject_id)
        .execution_options(populate_existing=True)
    )
    return dict(rows.tuples().all())


async def _tick_completed(db: AsyncSession, subject_id: int, report: Report) -> None:
    assignments = (
        (
            await db.execute(
                select(SubjectsAssignment)
                .where(SubjectsAssignment.subject_id == subject_id)
                .order_by(SubjectsAssignment.id)
            )
        )
        .scalars()
        .all()
    )
    for asg in assignments:
        keys = [i.key for i in settle_service.plan_for(asg).of("check")]
        if not keys:
            continue
        rows = (
            (
                await db.execute(
                    select(StudentAssignment)
                    .where(StudentAssignment.subjects_assignment_id == asg.id)
                    .order_by(StudentAssignment.id)
                )
            )
            .scalars()
            .all()
        )
        ticked: set[int] = set()
        for sa in rows:
            latest = await squads.latest_submission_for(db, sa.student_id, subject_id, asg.id)
            if latest is None or latest.status != SubmissionStatus.COMPLETED:
                continue
            for sa_id in await squads.scope_sa_ids(db, sa.student_id, subject_id, asg.id):
                if sa_id in ticked:
                    continue
                ticked.add(sa_id)
                member = await db.get(StudentAssignment, sa_id)
                if member is not None:
                    member.teacher_scores = {
                        **(member.teacher_scores or {}),
                        **dict.fromkeys(keys, 1),
                    }
        report.ticked += len(ticked)
    await db.flush()


async def _settle_all(db: AsyncSession, subject_id: int, report: Report) -> None:
    rows = (
        (
            await db.execute(
                select(StudentAssignment)
                .join(
                    SubjectsAssignment,
                    SubjectsAssignment.id == StudentAssignment.subjects_assignment_id,
                )
                .where(SubjectsAssignment.subject_id == subject_id)
                .order_by(StudentAssignment.id)
            )
        )
        .scalars()
        .all()
    )
    for sa in rows:
        asg = await db.get(SubjectsAssignment, sa.subjects_assignment_id)
        if asg is None:
            continue
        subs, _ = await settle_service.scope_submissions(db, sa, asg)
        if not subs:
            continue
        result = await settle_service.settle(db, sa)
        report.settled += 1
        if result.submission_id is not None and result.old_status != result.new_status:
            report.status_changes.append(
                (result.submission_id, str(result.old_status), str(result.new_status))
            )


async def backfill(db: AsyncSession, subject_id: int, *, apply: bool) -> Report:
    """Run the whole backfill in one savepoint. Writes persist in the caller's transaction
    only with ``apply`` and an empty ``grade_changes``; otherwise everything is undone."""
    report = Report()
    try:
        async with db.begin_nested():
            before = await _grades(db, subject_id)
            await _tick_completed(db, subject_id, report)
            await _settle_all(db, subject_id, report)
            after = await _grades(db, subject_id)
            report.grade_changes = [
                (sa_id, old, after.get(sa_id))
                for sa_id, old in before.items()
                if after.get(sa_id) != old
            ]
            if report.grade_changes or not apply:
                raise _Rollback
    except _Rollback:
        pass
    return report


def _print(report: Report, *, apply: bool) -> None:
    aborted = bool(report.grade_changes)
    mode = "APPLY" if apply else "DRY RUN"
    print(f"mode: {mode}{' - ABORTED, nothing written' if aborted else ''}")
    print(f"ticked: {report.ticked}")
    print(f"settled: {report.settled}")
    print(f"status changes: {len(report.status_changes)}")
    for sub_id, old, new in report.status_changes:
        print(f"  submission {sub_id}: {old} -> {new}")
    print(f"grade changes: {len(report.grade_changes)}")
    for sa_id, old_grade, new_grade in report.grade_changes:
        print(f"  sa {sa_id}: {old_grade} -> {new_grade}")
    if apply and not aborted and report.status_changes:
        print("note: settling may have enqueued teacher review notifications for those.")


async def _run(subject_id: int, apply: bool) -> int:
    async with get_session() as db:
        report = await backfill(db, subject_id, apply=apply)
    _print(report, apply=apply)
    return 1 if report.grade_changes else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    parser.add_argument("--subject", type=int, required=True, help="subject id")
    parser.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.subject, args.apply))


if __name__ == "__main__":
    sys.exit(main())
