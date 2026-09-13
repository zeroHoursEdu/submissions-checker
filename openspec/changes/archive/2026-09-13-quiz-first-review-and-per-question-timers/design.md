## Context

`submissions-checker` currently funnels every submission through the Docker sandbox.
`check_tasks.execute_check_task` pins the plugin config, calls
`check_core.resolve_check_plan`, and that function returns a `ConfigError` — surfaced as
`VALIDATION_FAILED` — when no `check_command` is configured
(`services/check_core.py:139-142`). Every `review_mode` name begins with `tests_`
(`workers/tasks/check_tasks.py:201-239`).

Quizzes are pure config: questions live in the raw `config.yml` blob on `SubjectPluginConfig`,
are drawn and snapshotted into `quiz_attempts.questions_snapshot` at attempt start, and are
graded from that snapshot (`api/routes/student_quiz.py:122-300`). Timing today is per attempt:
`time_limit_minutes` is snapshotted into `config_snapshot`, remaining time is computed server-side
from `quiz_attempts.started_at`, and the page renders one form containing every question with a
single countdown (`templates/student_quiz.html:50-220`).

The driving subject is a seven-lab WinAPI/C++ course whose labs cannot run on a Linux checker at
all, where the quiz replaces the oral defence and must be tight enough to be a real gate.

## Goals / Non-Goals

**Goals:**
- Accept an upload and go straight to a quiz, with no sandbox and no `check_command`.
- Optionally route a passed quiz to teacher review of the attached work.
- Time individual questions, with the server as the sole authority on elapsed time.
- Make a lost-to-the-clock question visibly different from a wrong answer.
- Break nothing: existing subjects (`cppBasics`, `e2e_test`) keep their current behaviour byte
  for byte.

**Non-Goals:**
- A manual grade-entry box on the teacher review screen. Approve/Reject stays as it is; the grade
  keeps coming from `finalize_grade`.
- Any change to how questions are authored, drawn, shuffled, or scored.
- Resumable/back-navigable timed quizzes, question flagging, or "review your answers" screens.
- Camera proctoring changes.

## Decisions

### D1. Branch before check-plan resolution, not inside it

The quiz-first modes are detected in `execute_check_task` immediately after the plugin config is
pinned and before `resolve_check_plan` is called. `check_core` stays untouched and DB-free, so the
standalone check-runner and its contract (`docs/runner-contract.md`) are unaffected — a subject
with no sandbox simply has nothing for the runner to validate.

*Alternative rejected:* teaching `resolve_check_plan` to return a "no-op plan". That would push a
review-mode concept into the DB-free check core and make every runner suite have to understand a
plan that runs nothing.

### D2. Still open the ZIP, just don't run it

Check-free does not mean unchecked input. `_accept_without_checks` extracts the archive with the
existing `safe_extract` (`utils/safe_zip.py`) into a `TemporaryDirectory` and throws the result
away, mapping `BadZipFile`/`UnsafeArchiveError` onto the existing `_fail_validation`. This reuses
already-tested zip-bomb and path-traversal defences and keeps the failure message shape identical
to every other subject. The cost is one extraction of a small report archive.

### D3. Reuse the existing state-machine events for entry, add a `QUIZ_SENT` row for exit

Entry needs no new events: `start_validation` → `validation_passed` → `test_passed_quiz` already
walks `PENDING → VALIDATING → TESTING → QUIZ_SENT` (`core/state_machine.py:12-27`). The exit does
need work, because `QUIZ_SENT` has no row in `_TRANSITIONS` at all today and `student_quiz.py`
mutates `submission.status` directly. Adding
`quiz_passed` / `quiz_passed_teacher` / `quiz_failed` and switching those call sites to
`transition()` closes a real hole rather than papering over it.

Passing through `TESTING` for a submission that ran no tests is a small lie in the status log; the
alternative — a `PENDING → QUIZ_SENT` shortcut event — skips the validation states that
`_fail_validation` depends on and would need its own error path. Reusing the existing walk keeps
one failure path for bad archives.

### D4. `review_mode` is snapshotted onto the attempt

`_grade_and_finalize` needs to know whether a pass completes the submission or hands it to a
teacher. Reading the live config there would let a mid-attempt config re-upload change the
outcome of an attempt already in flight. Every other quiz setting is already snapshotted into
`config_snapshot` at attempt start; `review_mode` joins them.

### D5. Stepper mode is derived, not declared

There is no `mode: stepper` config key. If any question drawn for the attempt resolves to a
`time_limit_seconds`, the attempt is a stepper attempt, and `config_snapshot["per_question_timing"]`
records that decision for the life of the attempt. A subject enables the stepper purely by giving
its questions time limits, and an existing subject that gives none is untouched. This also means
the decision is per attempt, not per quiz, so it cannot drift when the config changes.

### D6. Server-authoritative clock via a cursor on the attempt

Two new columns on `quiz_attempts` — `current_index` and `question_started_at` — hold the cursor
and the clock. The remaining time is always `limit - (now - question_started_at)`; the browser's
countdown is decoration. Every entry into the quiz page first runs `_advance_expired`, a loop that
burns each question whose window has closed (recording a `timed_out` answer with zero points).
Crucially, an expired question's successor starts its window when the expired one *ended*, not
"now" — otherwise a student who closes the laptop for an hour would lose exactly one question and
get a fresh clock on the next. Answering normally does restart the clock at "now", so finishing
early is never punished. Closing the laptop mid-attempt therefore costs exactly what staying on
the page costs.

*Alternative rejected:* storing a per-question deadline list in JSONB. Real columns are indexable,
migratable and cheap to reason about, and the cursor is a scalar, not a collection.

*Alternative rejected:* client-driven advance ("the JS tells us it expired"). Trivially defeated by
disabling JavaScript.

### D7. Post/Redirect/Get, with the index as the guard

Answering posts to a new `POST /portal/quiz/{attempt_id}/answer` carrying a hidden `index`. A
mismatch against `current_index` means a stale tab, a double submit, or a back-button replay; the
server ignores the body and redirects to the current question. The 303 back to `GET` after every
accepted answer means a refresh cannot resubmit. This is what makes "no going back" enforceable
rather than merely absent from the UI.

### D8. `timed_out` is a column on the answer, not a sentinel value

A question left to the clock and a question answered wrongly both score zero. The result page has
to tell them apart, and so does any later analytics. A boolean column on `quiz_answers` is the
honest representation; encoding it in the `answer` JSONB would make it invisible to SQL.

### D9. Extract the anti-cheat script before duplicating the page

`templates/student_quiz.html` carries roughly 250 lines of inline anti-cheat and proctoring JS.
The stepper page needs all of it. It moves to `templates/_quiz_anticheat.html` and both pages
`{% include %}` it — a mechanical extraction done in its own step so that a regression in the
stepper cannot be confused with a regression in the extraction.

### D10. `reduce_time` retargets to the current question in stepper mode

Deducting a penalty from a whole-attempt budget that no longer exists would be a no-op. In stepper
mode the penalty moves `question_started_at` backwards, which shortens the current question and
can expire it outright — the same felt consequence, scoped to where the student actually is. The
existing `violations["_time_penalty_seconds"]` accumulation stays for single-page attempts.

## Risks / Trade-offs

- **A slow network eats the student's clock.** The window starts when the server serves the
  question. → Windows are set per question by the teacher (30 s for recall, 60 s for hard items),
  not scraped to the second; a page load is a small fraction of that. Mitigation is calibration,
  not engineering.
- **`_advance_expired` runs on every page entry and writes rows.** A student who disappears for an
  hour burns the whole attempt in one request. → It is bounded by the number of drawn questions
  (15 for the driving subject), executed inside the request's existing transaction.
- **Passing through `TESTING` for a submission that never ran tests** may confuse anyone reading a
  raw status history. → `test_results` records `{"skipped": true, "reason": <mode>}`, so the reason
  is legible where it matters.
- **The teacher approve handler currently re-routes any assignment with a `quiz` block back to
  `QUIZ_SENT`** (`api/routes/teacher_portal.py:889-901`). Left alone, a `quiz_then_teacher`
  submission would loop between quiz and review forever. → Guard on "a passed attempt already
  exists". This is the single highest-risk edit in the change and gets a dedicated functional test.
- **Two delivery paths for one quiz feature.** Divergence between the single-page and stepper
  pages is a maintenance cost. → The scoring, drawing, anti-cheat and finalisation code is shared;
  only rendering and the submit endpoint differ, and the anti-cheat script is a single include.
- **`show_correct_answers_after` plus a 45-question pool.** Revealing answers hands a student
  material for their second attempt. → A config concern, called out in the authoring docs; the
  driving subject sets it to `false`.

## Migration Plan

One additive Alembic revision on top of `0022`: two nullable/defaulted columns on `quiz_attempts`
and one defaulted boolean on `quiz_answers`. No backfill — existing attempts are terminal or
single-page, and the defaults (`current_index = 0`, `question_started_at = NULL`,
`timed_out = false`) describe them correctly. Migrations run automatically at startup
(`core/migrations.py`). Rollback is the matching `downgrade` plus reverting the code; no data is
transformed, so nothing is lost either way. Existing subject configs need no edit.

## Open Questions

- Should an exhausted `max_quiz_attempts` under `quiz_then_teacher` fail the submission outright,
  or still hand the work to the teacher with a failed quiz attached? Current behaviour (fail) is
  retained for now; revisit once the subject has run a semester.
- Should analytics surface per-question timeout rates so a teacher can spot a window that is too
  tight? Deliberately deferred — the data (`quiz_answers.timed_out`) is being recorded so the
  question can be answered later without another migration.
