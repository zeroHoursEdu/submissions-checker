# Quiz any time + teacher-entered scores (`quiz_and_teacher_scores`) — design

Date: 2026-10-04 · Branch: `quiz-and-teacher-scores`

## Goal

A review mode for courses where the platform examines only the *defence* (a quiz) and the
teacher grades the *work itself* outside the platform. The student takes the quiz whenever
they like — no upload first. The teacher types per-criterion points on the assignment board.
The final grade is quiz points + teacher points, and the teacher (and the student) can see
which points came from where.

First user: `distributedBasics` (subject 1 on prod). Its "Принцип оцінювання": every lab =
report 5 + defence 8, labs 3–6 add a "з зірочкою" task (3/2/2/2). Today the platform grades
only the defence (`max_grade: 8`) and the report lives in the teacher's own sheet; the quiz
is locked behind a ZIP upload, which is why a student without an upload "has no access".

The app is live: 1 subject, 31 submissions, 34 quiz attempts (21 passed, all sitting in
AWAITING_TEACHER_REVIEW), 0 COMPLETED, no squads. Nothing may break for that data.

## Decisions (made with the owner)

| # | Decision |
|---|---|
| 1 | The student uploads **nothing** in this mode. Work is graded outside the platform; only the points are entered here. |
| 2 | Teacher points are **per criterion**, declared in `config.yml` (`report: 5`, `star: 3`). |
| 3 | The quiz is a **gate**: no passed attempt ⇒ no grade, whatever the teacher entered. Exhausted attempts ⇒ the existing "Додаткова спроба тесту" button. |
| 4 | Points are entered **inline on the assignment board**, one row per student, one save button per row. |
| 5 | Existing prod data **carries over**: passed attempts count, used attempts count toward the cap, old ZIPs stay as history. |
| 6 | Architecture: a quiz without an upload is anchored on an **upload-less Submission** (approach 1); `quiz_attempts.submission_id` stays NOT NULL. |
| 7 | Teacher points live in a new `students_assignments.teacher_scores` JSONB column. |
| 8 | The teacher may enter points before or after the quiz, and change them later; the grade appears once both halves exist. |

## 1. Config

```yaml
lab3:
  min_grade: 0
  max_grade: 16
  review_mode: quiz_and_teacher_scores
  grading:
    quiz_points: 8
    teacher_criteria:
      - { key: report, title: "Звіт", max: 5 }
      - { key: star,   title: "Завдання з зірочкою", max: 3, optional: true }
  quiz: { ... }            # required
```

- `optional: true` — the criterion may stay empty (counts as 0) without blocking the grade.
  The "з зірочкою" task is optional by nature; the report is not. Default `false`.
- `ConfigApplyService` rejects (with a readable message) a `quiz_and_teacher_scores`
  assignment when: no `quiz.questions`; `quiz_points` not a non-negative int; empty
  `teacher_criteria`; a key not matching `[a-z0-9_]+` or duplicated; a `max` not a positive
  int; `quiz_points + Σ max ≠ max_grade − min_grade`.
- The mode joins `_QUIZ_REACHABLE_MODES` and `_QUIZ_FIRST_MODES` (no sandbox ever runs).
- Every other mode is untouched, including `quiz_then_teacher`.

## 2. Student flow

Assignment page (`assignment_detail.html`) in this mode: no upload form; a quiz panel in one
of four states:

| State | Shows |
|---|---|
| not passed, attempts left | **«Почати тест»** + "спроб лишилось: N з M" |
| passed, teacher points incomplete | "Тест складено: 6/8. Очікуємо бали викладача" |
| exhausted, not passed | "Спроби вичерпано — зверніться до викладача" |
| COMPLETED | grade 12/16 + breakdown "Тест 6/8 · Звіт 4/5 · Зірочка 2/3" (always shown in this mode) |

**«Почати тест»** links to the existing start route `GET /portal/subjects/{subject_id}/assignments/{sa_id}/quiz`, which now, when there is no submission:

1. Rejects (409) unless the assignment's current mode is `quiz_and_teacher_scores`.
2. Locks the student's `students_assignments` row `FOR UPDATE` (double click ⇒ one submission).
3. If `squads.latest_submission(sa)` exists → no new row (an old ZIP submission on prod is
   reused, decision 5). Otherwise creates `Submission(source_type=QUIZ_ONLY,
   source_metadata={}, plugin_config_id=<latest config>, squad_id=<as at upload>)` and
   applies the new transition `quiz_opened` (PENDING → QUIZ_SENT). No outbox message.
4. Redirects to the existing quiz start route; everything after that is unchanged.

`SubmissionSourceType.QUIZ_ONLY` is a new value of the native PG enum (migration below).

**After the quiz** (`student_quiz.py` submit branch; `quiz_regrade.py` dispute branch):

- passed, teacher points complete → `quiz_passed` → COMPLETED + `finalize_grade`.
- passed, points incomplete → `quiz_passed_teacher` → AWAITING_TEACHER_REVIEW, teacher
  notified (as `quiz_then_teacher` does today).
- failed & exhausted → `quiz_failed` → FAILED (unchanged; grant button unchanged).

The branch decides by the **current** assignment mode, not by `config_snapshot.review_mode`,
so attempts started under the old `quiz_then_teacher` config land the same way. (Under
`quiz_then_teacher` the outcome is AWAITING_TEACHER_REVIEW either way, so the switch is
invisible to them.)

Squads: the first member to press the button creates the shared submission; the existing
per-member quiz split applies. Teacher points are written to every member's row (same as
the unified grade today).

## 3. Teacher board

`teacher_assignment.html`, when the assignment mode is `quiz_and_teacher_scores`, gets one
column per criterion and a total column; each row is a small form:

```
Студент          | Тест         | Звіт [_]/5 | Зірочка [_]/3 | Разом  | [Зберегти]
Духота Валентин  | 6/8 (75%) ✓  |     4      |       2       | 12/16  |
Іваненко Петро   | — не складено|     5      |               | —      |
```

- Quiz cell: points awarded (see §4) and pct of the passed attempt; "не складено (2/3)" or
  "—" otherwise. Every enrolled student has a row, with or without a submission.
- `POST /teacher/subjects/{subject_id}/assignments/{sa_id}/scores` with `student_id` and
  one field per criterion key. Guards: `require_subject_access`; the student is enrolled;
  the mode is this one. Each value: empty ⇒ unset, otherwise an int in `0..max` (else 422
  shown as a flash). Squad ⇒ written to every member's row.
- After saving: audit `teacher_scores_set` (old → new). Then, on the latest submission:
  - AWAITING_TEACHER_REVIEW and points complete → `teacher_approve` → COMPLETED,
    `finalize_grade`, student notified (existing "submission reviewed" notification).
  - COMPLETED → `finalize_grade` again (grade follows edits). Clearing a required
    criterion of a COMPLETED submission is refused (422) — a grade never disappears.
  - anything else (no submission, QUIZ_SENT, FAILED) → points just stored.
- The existing review page / bulk "approve" for a submission in this mode does **not**
  finalize without points: approve is refused (409) until points are complete. Reject is
  refused too (409): with nothing uploaded a rejected submission could never be reopened —
  low work points mark weak work instead. A legacy upload stuck in VALIDATION_FAILED /
  TEST_FAILED reopens as a fresh QUIZ_ONLY submission when the student opens the quiz.
- Студенти grid (`gradebook.py`): the cell keeps showing the grade; its title tooltip
  carries the breakdown line. `quiz_score` keeps being filled for the existing columns.

## 4. Grade

`grading.compute_grade` gets a branch for this mode (pure function, unit-tested):

```
quiz_awarded = round_half_up(quiz_pct / 100 * quiz_points)   # squad: ceil, as today
teacher      = Σ teacher_scores[k] for every criterion (optional & empty = 0)
grade        = clamp(min_grade + quiz_awarded + teacher, min_grade, max_grade)
```

`finalize_grade` reads `teacher_scores` from the owning `students_assignments` row and the
current `subjects_assignment.config` (as today). If the quiz is not passed or a required
criterion is empty it does nothing and returns None — callers above only call it when both
halves exist, this is a backstop.

`grade_breakdown` for this mode:

```json
{"grade": 12, "mode": "quiz_and_teacher_scores", "quiz_score": 75.0,
 "quiz": {"pct": 75.0, "points": 6, "max": 8},
 "criteria": [{"key": "report", "title": "Звіт", "points": 4, "max": 5},
              {"key": "star", "title": "Завдання з зірочкою", "points": 2, "max": 3}]}
```

## 5. Places that assume a ZIP

An upload-less submission must not reach code that reads a file:

- "Перезапустити перевірки" — hidden and refused (409) for `QUIZ_ONLY`.
- Review page download / file list — hidden for `QUIZ_ONLY`.
- Similarity already filters `source_type == ZIP_UPLOAD`; `cli/migrate_uploads.py` and the
  ZIP export are checked and filtered the same way if needed.
- The upload route refuses (409) a ZIP for an assignment in this mode.

## 6. Database & rollout (prod-safe)

Migration `0034_quiz_and_teacher_scores`, purely additive:

- `ALTER TYPE submissionsourcetype ADD VALUE 'QUIZ_ONLY'` (outside a transaction block,
  as PG requires) — exact type name taken from the DB.
- `students_assignments.teacher_scores JSONB NULL`.
- downgrade: drop the column; the enum value stays (PG cannot drop it; harmless).

No data is rewritten. Rollout order:

1. `run-prod-backup --now`.
2. Deploy the app (migration runs at startup). Old config v4 keeps working unchanged.
3. Release the new `distributedBasics` config (CI builds the ZIP) and Apply it. Order
   matters: the old app would reject the unknown mode.
4. Read-only check on prod: modes/max_grade per lab, the 21 waiting submissions visible on
   the boards with empty point fields.

Rollback: an image older than this release cannot load a `QUIZ_ONLY` row (its enum lacks
the value), so rolling back is safe only while `count(*) where source_type='QUIZ_ONLY'` is 0
— see docs/deployment.md › Rollback.

The 21 AWAITING_TEACHER_REVIEW submissions complete as soon as the teacher types their
points. QUIZ_SENT ones continue on their old submission. Students with nothing get «Почати тест».

## 7. Subject config (`basicsOfTheDistributedSoftwareDevelopment`)

All 7 labs → `review_mode: quiz_and_teacher_scores`, `quiz_points: 8`, criterion
`report: 5`; star criterion (`optional: true`) on lab3 (3), lab4–6 (2).
`max_grade`: 13, 13, 16, 15, 15, 15, 13 (= 100 total). Header comment and README
"Оцінювання" rewritten to match; `scripts/validate_config.py` taught the new mode if it
checks modes. `code_weight`/`quiz_weight` removed from those labs.

## 8. Testing

- Unit: `compute_grade` branch (rounding, optional criteria, squad ceil, clamp);
  config validation errors; `quiz_opened` transition.
- Functional (ASGI, auth on): student with no submission opens the quiz → one QUIZ_ONLY
  submission even on two posts; pass with/without points → right status; teacher saves
  points before/after the quiz; edit after COMPLETED re-grades; clearing required refused;
  approve on review page refused without points; rerun refused for QUIZ_ONLY; upload
  refused; another teacher's subject → 403; legacy ZIP submission in AWAITING_TEACHER_REVIEW
  completes when points are saved (decision 5).
- Migration: upgrade/downgrade on the testcontainer.
- Full suite, ruff, mypy, e2e smoke of the board.

## Out of scope

Per-criterion comments, CSV import of points, changing any other mode, a UI to edit
criteria (config ZIP only, by design).
