# Requirements list instead of review modes, batch scoring, code from GitHub

Date: 2026-10-17. Status: approved in chat section by section, awaiting written-spec review.
Supersedes `2026-10-15-grading-components-design.md`.

## Why

A `review_mode` is a fixed pipeline (`tests_then_ai_then_teacher`, `quiz_and_teacher_scores`,
…). Every new combination needs a new mode, a new set of state-machine transitions and new
branches in the workers. And it gets the teacher's role wrong: in the distributed-C++ subject a
lab counts only when three things are done — the defence quiz passed, the report scored, and
the teacher personally accepting the work after an oral defence. Today the third one is not
part of the grade model: the board's **Затвердити** button is a workflow action, and saving
complete scores after a passed quiz completes the submission on its own.

The fix is to stop describing a pipeline and declare instead **the list of things that must be
done for the work to count**. Each item is independent and can be done in any order (the quiz
may be passed before anything is handed in). The grade appears once every required item is
done, and is the sum of the items' points.

Two more gaps showed up while grading the first labs by hand:

- Scores are entered one student at a time; a 40-student lab means 40 form posts.
- Students were asked to keep code out of the report and push it to a public GitHub repo. The
  LLM grader only sees the Classroom files, so "the implementation matches the variant" is
  judged blind.

## Scope

1. `requirements:` list replaces `review_mode` for every assignment; old configs are translated
   on load.
2. One evaluation function computes each item's state; one `settle` function sets grade and
   status after every event. Mode-specific transitions are removed.
3. Teacher board: one column per item, a `check` item for acceptance (locked once saved),
   batch save, «Взяти бали з AI-чернетки». **Затвердити** is removed everywhere.
4. Student assignment page shows a checklist.
5. LLM grader receives the student's GitHub code from a link in the report; nothing is kept on
   disk.
6. Subject repo config v8, backfill, rollout.

Out of scope (next step): running `tests` / `ai_review` on Classroom works (needs assembling a
sandbox directory from Drive files + GitHub). Private repos, a GitHub field in the profile.

## 1. Config

```yaml
lab4:
  min_grade: 0
  max_grade: 16
  source: classroom            # upload | classroom
  requirements:
    - {type: quiz,      key: quiz,     points: 8}
    - {type: score,     key: report,   title: "Звіт", max: 5, llm: *llm_report}
    - {type: score,     key: star,     title: "Зірочка", max: 3, optional: true}
    - {type: check,     key: accepted, title: "Прийнято викладачем", points: 0}
    # subjects with code:
    - {type: tests,     key: tests,    points: 10}
    - {type: ai_review, key: ai,       points: 2}
```

| type | done when | points earned |
|---|---|---|
| `quiz` | a passed attempt exists | `points × pct/100` (squad: mean over members, as today) |
| `score` | the teacher saved a value | 0..`max` |
| `check` | the teacher ticked it and pressed Save; cannot be unticked | `points` (0 allowed) |
| `tests` | sandbox check of the latest version finished | `points × score/max_score` |
| `ai_review` | AI review of the latest version finished | `points × code_mark/100` |

- `optional: true` — not needed for the grade, only adds points (allowed on any type).
- `source`: `upload` = student uploads a ZIP on the portal; `classroom` = works come from the
  Classroom sync (Drive files + GitHub link), the portal shows no upload. Default: `upload` when
  the list has `tests` or `ai_review`, otherwise none (no hand-in at all).
- Validation in `config_apply`:
  - `Σ points/max == max_grade - min_grade`; keys unique; `title` required for `score`/`check`.
  - `quiz` requires a `quiz:` block; a `quiz:` block without a `quiz` item is refused.
  - `check` takes `points`, not `max`, and no `llm`.
  - `tests`/`ai_review` with `source: classroom` is refused ("not supported yet").
  - `review_mode` together with `requirements` is refused; `review_mode` alone is translated.
- Grade = `min_grade + Σ earned`, rounded once at the end (squads: ceil, `round(…, 6)` first as
  today), clamped to `[min_grade, max_grade]`.

### Translating old configs

`services/requirements.py: from_config(assignment_config) -> list[Requirement]`, the only
reader of `review_mode` left.

- `quiz_and_teacher_scores` → `quiz(points=quiz_points)` + each `teacher_criteria` entry as
  `score`. Grades are identical to today's `teacher_scores.compute`.
- Weighted modes → `tests` / `ai_review` / `quiz` with `points = (max−min) × w / Σw`, where `w`
  is the component's effective weight (`code_weight × works_weight`, `code_weight ×
  quality_weight`, `quiz_weight`) and only components the mode actually runs are included.
  Modes containing `teacher` add `check accepted, points 0`. `quiz_only` → `quiz` with all
  points. `source: upload` for modes with tests.
- Known difference: the old blend renormalised weights when a component had no data. In the new
  model a required item without data means no grade yet; the only practical case (AI review
  failed) was already a non-completed state.

## 2. Evaluation and settle

The unit evaluated is the student's assignment (`StudentAssignment`; for a squad, all members
together via `squads.scope_sa_ids`), not one submission — items are independent, so a new
upload must not wipe a passed quiz or saved scores.

Facts per item:

- `quiz` — best passed attempt on this assignment across the scope's submissions; failed when
  no attempts are left (a granted attempt makes it pending again).
- `tests`, `ai_review` — the latest submission's `test_results` / `ai_review`; `running` while
  it is in `PENDING…AI_REVIEWING`; `tests` failed on `TEST_FAILED`/`VALIDATION_FAILED`.
- `score`, `check` — `StudentAssignment.teacher_scores` (JSONB, `dict[str, int]`; a check is
  stored as `1`). Teacher items never fail.

`requirements.evaluate(reqs, facts) -> Evaluation` is pure: per item `state` (`done | pending |
running | failed`) and `points`; overall `complete`, `failed`, `grade`, `breakdown`.

`requirements.settle(db, sa)` is called after every event: quiz submitted or regraded, check
task finished, AI review finished, teacher Save (single or batch), teacher reject, new Classroom
version, granted attempt. It loads facts, evaluates, then on the scope's latest submission:

- complete → write grade + `grade_breakdown` to every member, transition `requirements_met` →
  `COMPLETED`;
- a required item failed → no grade, `requirement_failed` → `FAILED`;
- otherwise → no grade, `awaiting_teacher` → `AWAITING_TEACHER_REVIEW` when a teacher item is
  pending, else `awaiting_quiz` → `QUIZ_SENT`.

State machine: processing transitions stay (`PENDING → VALIDATING → TESTING → AI_REVIEWING`,
failures, retry, requeue). Mode-specific ones go: `test_passed_tests_only/ai/teacher/quiz`,
`ai_review_done_teacher/completed`, `ai_review_passed_quiz`, `quiz_passed`,
`quiz_passed_teacher`, `teacher_approve`, `teacher_send_quiz`, `dispute_regrade_passed*`. New:
`processing_done` (end of the upload pipeline, then `settle`), `requirements_met`,
`requirement_failed`, `awaiting_teacher`, `awaiting_quiz` — allowed from every settled state,
so `settle` is idempotent. `teacher_reject` stays (`FAILED`, with reason); the next version
settles again.

`finalize_grade` becomes a thin wrapper over `settle` for the remaining callers;
`teacher_scores.py` keeps form parsing and is otherwise absorbed by `requirements.py`.
`check_tasks._advance_after_tests`, `_accept_without_checks` and the mode branches in
`student_quiz` / `quiz_regrade` are replaced by `settle`.

After `COMPLETED` the grade is fixed: a new Classroom version only refreshes the AI draft and
the "new version" badge; a changed score on the board re-settles and recomputes.

## 3. UI

### Teacher board (assignment page)

- One column per item. `quiz`/`tests`/`ai_review`: read-only badge (✓/✗/⏳/—) + points; the quiz
  badge links to the attempt. `score`: number input with the AI-draft value as a hint. `check`:
  a checkbox while unsaved; once saved it renders as plain text «✓ 17.10» with no input at all.
  The server ignores any attempt to clear a saved check (stale tab). Last column: grade or «—».
- All rows join one form, posting to `POST /teacher/subjects/{id}/assignments/{sa}/scores/batch`.
  Field names `s<student_id>__<key>`, `llm_grading_id__<student_id>`, `orig__<student_id>`
  (hash of the row as rendered). Only rows whose values differ from `orig` are processed. Each
  row runs the per-row logic (extracted from `teacher_save_scores` into a service function) in
  its own savepoint: link-confirm and stale-draft checks, squad fan-out, draft approval, audit
  `teacher_scores_set`, `settle`. A failing row does not roll back the others. Flash: "saved N"
  plus each skipped student with the reason.
- «Взяти бали з AI-чернетки»: copies the selected rows' draft points into their inputs
  client-side; nothing is saved until «Зберегти зміни».
- **Затвердити** is removed: row button, bulk action, `POST /teacher/submissions/{id}/approve`
  and the bulk `approve` action. **Відхилити** stays (row + bulk). The per-row save button is
  removed (one save for the table).
- Filter "not done: <item>" shows only rows where that required item is not done.

### Student assignment page

```
ЛР4 — Сокети                      Оцінка: — (з'явиться, коли все виконано)
 ✓ Тест (квіз)            7/8
 ○ Звіт                   очікує оцінки викладача
 ○ Зірочка (необов'язково)
 ○ Прийнято викладачем    захисти роботу викладачу
```

- The quiz button is available whenever attempts are left, regardless of other items.
- ZIP upload only for `source: upload`; `classroom` shows «Здавай у Google Classroom» + link.
- `score` values appear only after the teacher saved them. The AI draft is never shown or hinted
  to students.
- Gradebook (Студенти tab) is unchanged; `_next_step_for` shows the first pending required item.

## 4. Code from GitHub for the LLM grader

New module `services/llm_grading/github_code.py`, used by `runner.build_request`:

1. **Find the link** in the work's gradable files: DOCX via `zipfile` (`word/document.xml` text +
   `word/_rels/document.xml.rels` targets), PDF via `pypdf` (page text + `/URI` annotations; new
   dependency). The first `https://github.com/<owner>/<repo>[/tree/<ref>[/<path>]]` wins.
2. **Fetch** `GET https://api.github.com/repos/<owner>/<repo>/tarball/<ref or default>` (httpx,
   follow redirects, 20 s timeout, no token), streamed into memory and aborted past 20 MB.
3. **Unpack in memory** with tar guards (no absolute or `..` paths, no links, regular files
   only). Keep files under `<path>`; extensions `.cpp .cc .c .h .hpp .rc .txt` plus
   `CMakeLists.txt`, `.sln`, `.vcxproj`; skip `.git/ x64/ Debug/ Release/ .vs/ build/`. Caps:
   200 files, 1 MB; the rest is listed as skipped.
4. **Send** the files as attachments named `github/<path>`; the prompt gains a CODE section
   (repo, resolved commit SHA, file list, "judge the implementation from this code; if absent,
   say so").
5. **Nothing is kept.** The code never touches disk, MinIO or the DB; buffers are released once
   the request is built. If a temporary file is ever needed it lives in a `TemporaryDirectory`
   context (removed on error too). Only the manifest is stored:
   `llm_gradings.draft.code_source = {url, commit, files: [{path, size}], skipped, error}`, shown
   on the report page. Re-fetching by commit SHA reproduces the same code.
6. **Failure is not an error.** No link, 404, private repo, rate limit, oversized: the draft is
   graded on the report alone and `code_source.error` names why. GitHub is not retried.
   Unauthenticated limit 60 req/h per IP; a nightly run stays well below it.

## 5. Migration, config v8, rollout

- No schema change: `teacher_scores` keeps its JSONB shape; a check is `accepted: 1`.
- Backfill script (prod one-off, dry-run first): for every `COMPLETED` submission of an
  assignment with a `check` item, set the check to `1` on every member's row (they were accepted
  through the old Затвердити); then `settle` every student assignment of the subject and assert
  no grade changed — any change aborts without writing. Submissions stuck in removed transitions
  (e.g. `AWAITING_TEACHER_REVIEW` after a quiz in scored mode) are settled into the right state.
- Subject repo config v8: labs 1–7 get `source: classroom` and `requirements` = `quiz` 8 +
  `report` 5 (+ optional `star` 3 from lab3) + `accepted` (check, 0). `_llm_instructions` and
  report requirement 3 say the code is taken from the GitHub link in the report; lab
  descriptions tell students not to paste code into the report but link a public repo.
- Rollout (backup before each step):
  1. Merge + deploy the backend. v7 is translated (no `accepted`), so behaviour and grades are
     unchanged; verify on prod by re-settling read-only.
  2. Apply config v8.
  3. Run the backfill.
  4. Current lab4 squads 1 and 4 show up as "pending: Прийнято викладачем".

## Testing

- Unit: requirement parsing/validation; translation of every old mode (grades equal to the old
  `compute_grade` / `teacher_scores.compute` on a table of cases); `evaluate` per item type and
  state; squad mean + ceil; GitHub URL extraction (DOCX text, DOCX rels, PDF text, PDF
  annotation, tree/path forms); tar guards, filter and caps; no temp files left after a run.
- Functional: batch save (changed rows only, partial failure, squad fan-out, stale draft); saved
  check renders without an input and a POST clearing it is ignored; quiz passed before any
  hand-in, grade appears on the last item; reject; Затвердити routes gone (404); student
  checklist, no AI draft leak; runner attaches GitHub files (fake HTTP transport) and degrades
  on 404.
- Regression: the existing tests for the old modes pass through the translation.
- Config apply: v8 validates; `check` with `max`/`llm`, `review_mode`+`requirements`,
  `tests`+`classroom` are refused.
