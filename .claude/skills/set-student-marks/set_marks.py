"""Set teacher scores for named students on prod, through the same service the teacher board uses.

Run INSIDE the prod app container, from the repo root:

    scripts/ops/prod-compose.sh exec -T app python - < .claude/skills/set-student-marks/set_marks.py          # dry run
    scripts/ops/prod-compose.sh exec -T app python - --apply < .claude/skills/set-student-marks/set_marks.py  # write

Edit JOBS first. Dry run (default) always rolls back and prints the grade each job would give.
`save_row` stores the scores, settles the work (grade, status, grade_breakdown), shares scores
across squad members and queues the "work reviewed" e-mail. Never write `grade` with raw SQL.
"""

import asyncio
import sys
from dataclasses import dataclass

from sqlalchemy import select

from submissions_checker.db.models import StudentAssignment, SubjectsAssignment
from submissions_checker.db.session import get_session
from submissions_checker.services import board_scores
from submissions_checker.services.llm_grading.board import llm_board_state

APPLY = "--apply" in sys.argv

# (student_id, subjects_assignment_id, scores). One member per squad is enough.
# Keys: "report" (0-5), "star" (0-2/3, only when the teacher said it was done).
# "accepted" is added automatically. Subjects_assignment ids 1..7 = ЛР1..ЛР7 of subject 1.
JOBS: list[tuple[int, int, dict[str, str]]] = [
    # (42, 1, {"report": "4"}),
]


@dataclass
class Teacher:
    user_id: int = 1  # users.id of the teacher account (kfeshchenko)
    username: str = "kfeshchenko"


class Rollback(Exception):
    pass


async def main() -> None:
    try:
        async with get_session() as db:
            for sid, aid, scores in JOBS:
                asg = await db.get(SubjectsAssignment, aid)
                state = (await llm_board_state(db, asg, [sid]))[sid]
                draft_id = state["draft_grading_id"]
                form = {f"score_{k}": v for k, v in scores.items()} | {"score_accepted": "1"}
                res = await board_scores.save_row(
                    db, asg, sid, form, prefix="score_",
                    llm_grading_id=str(draft_id) if draft_id else None,
                    actor=Teacher(),
                )
                sa = await db.scalar(
                    select(StudentAssignment)
                    .where(StudentAssignment.student_id == sid,
                           StudentAssignment.subjects_assignment_id == aid)
                    .execution_options(populate_existing=True)
                )
                quiz = [round(i.points, 2) for i in res.evaluation.items if i.key == "quiz"]
                print("saved", sid, aid, "quiz", quiz, "grade", sa.grade,
                      "status", res.new_status.value, "scores", sa.teacher_scores)
            if not APPLY:
                raise Rollback()
    except Rollback:
        print("DRY RUN - rolled back")


asyncio.run(main())
