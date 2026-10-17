# Teacher acceptance as a grade component, batch scoring, code from GitHub

Date: 2026-10-15. Status: SUPERSEDED by `2026-10-17-requirements-list-design.md`.

## Why

In `quiz_and_teacher_scores` mode a lab's grade is meant to need three things: the defence
quiz passed, the report scored, and the teacher personally accepting the work after an oral
defence. Today the third one is not part of the grade model at all: the board's
**Затвердити** button is a workflow action that flips the submission to `COMPLETED`, and saving
complete scores after a passed quiz also completes it on its own. So "accepted by the teacher"
is neither declared in the subject config nor required for a grade.

Two more gaps showed up while grading the first three labs by hand:

- Scores are entered one student at a time; a 40-student lab means 40 form posts.
- Students were asked to keep code out of the report and push it to a public GitHub
  repository instead. The LLM grader only sees the Classroom files, so criterion 3 ("the
  implementation matches the variant, as seen from the code") is judged blind.

## Scope

1. A `check` criterion type in `grading.teacher_criteria`: a teacher-only yes/no component
   that can carry points (0 allowed). Required checks gate the grade like required scores.
2. The **Затвердити** action disappears for `quiz_and_teacher_scores` assignments (row and
   bulk). Acceptance is the check. Other review modes keep the button unchanged.
3. Batch score entry on the assignment board: one form, one "save all changed rows" button.
4. The LLM grader receives the student's GitHub code, found via a `github.com/...` link in
   the report text.
5. Subject repo: config gains the `accepted` check on all 7 labs; LLM instructions and lab
   descriptions say code lives on GitHub, linked from the report.

Out of scope: private repositories (no token), a GitHub field in the student profile, Classroom
link attachments, changing other review modes.

## 1. The `check` criterion

Config:

```yaml
grading:
  quiz_points: 8
  teacher_criteria:
    - {key: report, title: "Звіт", max: 5, requirements: *llm_report}
    - {key: accepted, title: "Прийнято викладачем", type: check, points: 0}
```

- `type` defaults to `score` (today's behaviour). `type: check` takes `points` (int ≥ 0)
  instead of `max`; `requirements` is not allowed; it is never sent to the LLM (implicitly
  `llm: false`). `optional: true` is allowed and means the check only adds points.
- Validation: the total becomes `quiz_points + Σ score.max + Σ check.points ==
  max_grade - min_grade`. Keys stay unique across both types.
- Storage: `students_assignments.teacher_scores[key] = 1` when checked; the key is absent when
  not. No schema migration — same JSONB column, still `dict[str, int]`.
- Form parsing: a check arrives as `score_<key>=1` (checkbox) or is missing (unchecked).
- `is_complete`: every required score criterion has a value **and** every required check is 1.
- `compute`: a checked check adds its `points`; the breakdown lists it like a criterion with
  `points`/`max` both equal to its points (0/0 renders as "✓").
- Squads: unchanged — the scores write fans out to every member, so acceptance is shared.

### Completion and revocation

- The existing save path already completes a submission when the quiz is passed and scores are
  complete (`teacher_portal.teacher_save_scores`). With the check required, "complete" now
  includes acceptance, so nothing completes until the teacher ticks it.
- Unticking a required check on a `COMPLETED` submission is allowed (today removing a required
  score there is a 422). New transition `COMPLETED → AWAITING_TEACHER_REVIEW` named
  `teacher_revoke`; the grade on every member's `students_assignments` row is cleared and
  `grade_breakdown` is set to null. Removing a required *score* keeps the current 422.
- A quiz pass that lands after the scores and the tick completes the submission exactly as
  today (`quiz_open.scores_complete_for` → `is_complete`).

### Затвердити

- For `quiz_and_teacher_scores` assignments the board hides the row and bulk **Затвердити**
  buttons, and `POST /teacher/submissions/{id}/approve` plus the bulk `approve` action answer
  409 ("acceptance is a grade criterion in this assignment"). **Відхилити** stays.

## 2. Batch scoring

- The board's score inputs (and the new checkbox) for all rows join one form posting to
  `POST /teacher/subjects/{id}/assignments/{sa}/scores/batch`. Field names carry the
  student: `s<student_id>__<key>`, plus `llm_grading_id__<student_id>` and
  `orig__<student_id>` (a hash of the row's values as rendered).
- Only rows whose posted values differ from `orig` are processed. Each row runs the existing
  per-row logic (extracted from `teacher_save_scores` into one service function) inside its
  own savepoint: link-confirm and stale-draft checks, squad fan-out, draft approval, audit
  `teacher_scores_set`, completion/revocation.
- A failing row does not roll back the others. The redirect flashes "saved N", plus each
  skipped student with the reason.
- **Взяти бали з AI-чернетки**: a button that copies each selected row's draft points into its
  inputs client-side (no save). The per-row save button stays.

## 3. Code from GitHub for the LLM grader

New module `services/llm_grading/github_code.py`, used by `runner.build_request`:

1. **Find the link.** Extract text and hyperlinks from the work's gradable files: DOCX via
   `zipfile` (`word/document.xml` text + `word/_rels/document.xml.rels` targets), PDF via
   `pypdf` (page text + `/URI` link annotations). The first
   `https://github.com/<owner>/<repo>[/tree/<ref>[/<path>]]` wins.
2. **Fetch.** `GET https://api.github.com/repos/<owner>/<repo>/tarball/<ref or default>`
   (httpx, follow redirects, 20 s timeout, no token). Unpack in memory with
   `utils/safe_zip`-style guards (tar variant: no absolute/`..` paths, no links).
3. **Filter.** Keep files under `<path>` if given; extensions `.cpp .cc .c .h .hpp .rc .txt`
   plus `CMakeLists.txt`, `.sln`, `.vcxproj`; skip `.git/`, `x64/`, `Debug/`, `Release/`,
   `.vs/`, `build/`. Caps: 200 files, 1 MB total; past the cap, the rest are listed as
   skipped.
4. **Send.** Files go to the judge as extra attachments named `github/<path>`. The prompt
   gains a CODE section: repo, resolved commit SHA, file list, and the rule "judge criterion 3
   from this code; if absent, say so".
5. **Record.** `llm_gradings.draft.code_source = {url, commit, files, skipped, error}`; the
   teacher's report page shows it.
6. **Failure is not an error.** No link, 404, private repo, rate limit, oversized: the grading
   still runs on the report alone and `code_source.error` names why. Nothing retries GitHub.

Unauthenticated limit is 60 req/h per IP; a nightly run grades well under that.

## 4. Subject repo and rollout

- `config.yml`: `accepted` check (`points: 0`) on labs 1–7; sums unchanged.
  `_llm_instructions` and the report requirement (item 3) say code is taken from the GitHub
  link in the report. Lab descriptions tell students the same. Release **v8**.
- Rollout order (backup before each step):
  1. Merge + deploy the backend. Config v7 has no checks, so nothing changes yet.
  2. Apply config v8.
  3. One-off backfill: every `COMPLETED` submission of a lab with the check gets
     `accepted = 1` on all members' rows (it was accepted via the old flow). Re-run
     `finalize_grade` and assert no grade changed.

## Testing

- Unit: criterion parsing/validation for `check`, `is_complete`/`compute` with checks,
  form parsing, GitHub URL extraction (DOCX, PDF text, PDF annotation, tree/path forms),
  tar filtering and caps.
- Functional: batch save (changed-only, partial failure, squad fan-out, stale draft),
  completion only after the tick, revoke clears the grade, approve hidden/409 in scored mode,
  runner attaches GitHub files (fake HTTP transport) and degrades on 404.
- Config apply: v8 validates; a check with `max` or `requirements` is refused.
