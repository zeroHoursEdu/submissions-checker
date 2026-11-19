---
name: set-student-marks
description: Set final lab marks for named students on production (report score, star task, acceptance) — use when the teacher pastes a list of who passed/submitted which lab and wants grades put in, e.g. "Козлов/Сокуренко 5, 6, 7 лаби", "виставити оцінки", "постав їм".
---

# Set student marks on prod

The teacher pastes chat lines like `Ковтун - Трісунов 3-4 лаба зараховано`, `… зробили із зірочкою`,
`… на макс бали`, `… середні бали`. Turn them into grades. Terse Ukrainian user: act, then report a table.

## Grading policy (chosen by the teacher, Nov 2026)

Grade = **quiz points + report points (+ star)**, computed by `settle`, not typed.
- Subject 1 labs = `subjects_assignments` ids 1..7. Max: ЛР1,2,7 = 13 (quiz 8 + report 5);
  ЛР3 = 16 (star 3); ЛР4,5,6 = 15 (star 2). Quiz is always 8 points, from the quiz result.
- **Report: 75–100 % of 5, i.e. 4 or 5** by default. Start from the LLM draft (`llm_gradings.draft`),
  but never below 4 for work the teacher says is accepted. Squad with different drafts: take the lower.
  Teacher override words: «макс бали» → report 5 + star (max for the lab); «високі» → 5;
  «середні бали» → ≈75–80 % of max (e.g. ЛР2 → 10 = 8+2, ЛР3 → 12, ЛР4 → 11); «трохи вище мінімуму» →
  min+1, min = 60 % of max (ЛР1,2,7 → 9; ЛР4,5,6 → 10). Reach it by lowering `report`, since `grade` is derived.
- **Star only when the teacher says it was done**; value from the LLM draft, else full.
- **Never touch a lab that already has a grade** ("якщо вже є оцінка то нічого не чіпаєш").
- **Squads: both members always get the same grade.** One shared submission; `save_row` for one
  member updates both. Verify afterwards (query in `prod-grades-queries`: squad parity must return 0 rows).
- **Cannot be graded** (tell the user, skip): no submission, or quiz not passed (squads need *both* members
  to have passed → otherwise status stays QUIZ_SENT and grade NULL). Don't invent grades for work not handed in.
  Never `UPDATE students_assignments SET grade` directly (that once put grades on labs never submitted).

## Procedure

1. Find students and state with the queries in `prod-grades-queries` (grade, teacher_scores, submission ids,
   quiz per submission, LLM drafts). Match names carefully (DB is "First Last"; sheet is "Last First Patronymic").
2. Decide each (student, lab) → scores. Skip graded labs; list the ungradeable ones.
3. Fill `JOBS` in `set_marks.py` (copy to the scratchpad, don't commit edits), run the **dry run**, check that
   each grade is what you expect (quiz points can be fractional: 14/15 → 7.47; squad quiz = mean of members).
4. Backup: `scripts/ops/run-prod-backup.sh --now`. Then run with `--apply`.
5. Verify: re-query the grades and the squad-parity query.
6. Report a table (student, lab, grade, composition) plus what was skipped and why. Offer the sheet sync
   (`sync-grades-to-sheet`).

Env: all scripts need `PROD_ENV_FILE=<path to ssh/prod.env>` (in a worktree the file lives in the main
checkout: `/home/vampir/petProjects/submissions-checker/ssh/prod.env`).

Side effect to mention: each newly completed work queues `SUBMISSION_REVIEWED`; the outbox sends every
squad member an e-mail ("work approved", no grade in the text) and an in-app notification. Verified working.
