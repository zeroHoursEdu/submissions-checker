# Teacher grants one extra quiz attempt — design

**Date:** 2026-09-21
**Status:** approved for implementation (autonomous session; open decisions were delegated
to the implementer in the request, assumptions are listed at the end)

## Problem

A student who uses up `max_quiz_attempts` without passing lands in `FAILED` through the
`quiz_failed` event. `start_or_resume_quiz` then refuses the quiz with 403 because the
submission is no longer `QUIZ_SENT`. A teacher who wants to give that one student one more
try has no in-app way to do it. The only workaround is DB surgery (delete a
`quiz_attempts` row, hand-edit `submissions.status`), which bypasses the audit log,
destroys history and violates the "never assign `submission.status` directly" invariant.

## Goals

- One teacher action, on the assignment board, grants exactly one more attempt to one
  student on one submission.
- Offered only when the submission is stuck for that reason; never on submissions failed
  by a teacher rejection or already completed.
- Goes through an explicit state-machine event; keeps every existing `quiz_attempts` row.
- Audited like the other unstick controls; the student is told in-app.
- Works for squad-shared submissions per member.

## Non-goals

- Granting more than one attempt per click (click again to grant another).
- Email notification (no unstick control emails today; a new outbox event type would need
  an enum migration).
- Bulk grant from the board's checkbox form.
- Resetting a squad draw or editing squad membership.

## Storage

`submissions.source_metadata["quiz_extra_attempts"]` — a JSON object mapping the student
id (as a string, JSON keys are strings) to the number of extra attempts granted:

```json
{"quiz_extra_attempts": {"17": 1}}
```

Why here and not a column or table:

- It is scoped to the submission. A re-upload creates a new `submissions` row and the
  allowance naturally does not carry over — the new submission gets the config's base
  count again, which is what a fresh upload means everywhere else.
- It is per student, which is what a squad-shared submission needs (each member has their
  own attempt count and their own exhaustion).
- `source_metadata` already carries per-submission quiz state (`squad_quiz_draw`), written
  with the same "reassign the dict" JSONB change-tracking pattern.
- Who granted it and when lives in `audit_logs`, as for every other teacher action.

## State machine

New event `quiz_attempt_granted` in `core/state_machine.py`:

| From | To | Why |
|---|---|---|
| `FAILED` | `QUIZ_SENT` | The normal case: exhausted, submission failed, teacher re-opens the quiz. |
| `QUIZ_SENT` | `QUIZ_SENT` | Squads only. Member A exhausts → whole squad `FAILED`. Member B was mid-attempt at that moment, finishes it, and is now also exhausted. Teacher grants A (→ `QUIZ_SENT`); B still needs a grant while the submission is already `QUIZ_SENT`. The self-loop keeps one code path (`transition()` is always called) and makes the edge explicit in the table. |

Not reachable from `COMPLETED`, `AWAITING_TEACHER_REVIEW` or any other status.

## Precondition ("grantable")

A `(submission, student)` pair is grantable when **all** hold:

1. `submission.status` is `FAILED` or `QUIZ_SENT`.
2. The pinned plugin config (`submission.plugin_config`) has a quiz for the assignment
   with `max_quiz_attempts` set (`base`). No cap → nothing to grant.
3. The student is on the submission: the `students_assignment` owner for a solo
   submission; a currently enrolled squad member for a squad submission.
4. Looking at that student's attempts on the submission (`student_id == student` or
   legacy `NULL`, same filter as `start_or_resume_quiz`):
   - at least one attempt exists and every attempt is terminal
     (`COMPLETED | TIMED_OUT | VIOLATION_FAIL`);
   - none is `is_passed`;
   - `used >= base + extra_already_granted`, i.e. the student really is exhausted.

Why this identifies "failed because the quiz was exhausted" without a separate marker:

- `teacher_reject` happens from `AWAITING_TEACHER_REVIEW`. Under `tests_then_teacher`
  modes that is before any quiz, so there are no attempts (rule 4 fails). Under
  `quiz_then_teacher` the quiz already passed, so a passed attempt exists (rule 4 fails).
- `FAILED` is otherwise terminal, so nothing else produces `FAILED` with only failed,
  exhausted attempts.

## Effective attempt cap

`effective_max = base + extra_attempts(submission, student)` replaces the raw
`max_quiz_attempts` in the three places that gate on it:

| Place | Today | After |
|---|---|---|
| `student_quiz.start_or_resume_quiz` (~line 764) | `used >= max_attempts` → redirect to last result | `used >= effective_max` |
| `student_quiz._grade_and_finalize` (~line 639) | `prior + 1 >= max_attempts` → `quiz_failed` | `prior + 1 >= effective_max`, `attempts_left` from the same |
| `student_portal.assignment_detail` (~line 360) | `quiz_max_attempts` from the attempt snapshot / config | same source, plus extra |

The per-attempt `config_snapshot["max_quiz_attempts"]` keeps recording the **base** value
from the config. Snapshotting the effective value would double-count on a second grant
(snapshot already includes extra 1, then extra becomes 2).

After the granted attempt is used and failed, `used == base + extra`, the finalize check
fires `quiz_failed` again and the submission returns to `FAILED`, grantable again. Each
click gives exactly one more attempt.

## Service: `services/quiz_grants.py`

```python
def extra_attempts(submission, student_id) -> int
def effective_max_attempts(base: int | None, submission, student_id) -> int | None
async def grantable_students(db, submission) -> list[int]      # ids that satisfy the precondition
async def grant_extra_attempt(db, submission, student_id) -> int  # returns new extra count
class GrantError(Exception)                                    # precondition not met
```

`grant_extra_attempt` re-checks the precondition, bumps the counter (reassigning
`source_metadata`), calls `transition(submission, "quiz_attempt_granted")`, and does
**not** commit — the route owns the transaction, as with `_requeue_checks`.

Nothing in the service deletes or edits `quiz_attempts` rows.

## Route

`POST /teacher/submissions/{submission_id}/grant-quiz-attempt`
form field `student_id: int` (optional; defaults to the submission's `students_assignment`
student, which is always right for a solo submission).

- Auth: `_load_submission_for_teacher` (owner or ADMIN), like the other unstick routes.
- `GrantError` → `409 "Submission is not in a state that allows this action"`.
- On success, in one transaction:
  - `audit(action="grant_quiz_attempt", target_type="submission", target_id=…,
    student_id=…, extra_attempts=<new total>)`;
  - `push_notification` to the student's user (if one exists) with
    `vocab.quiz.notif_extra_attempt_title/body`, linking to their assignment detail page
    (`/portal/subjects/{subject_id}/assignments/{their sa_id}`);
  - commit, redirect 303 to the board (`_board_url`).

## Board UI

`teacher_assignment` computes `grantable: set[tuple[int, int]]` of
`(submission_id, student_id)` for rows whose status is `FAILED`/`QUIZ_SENT`, by loading
those submissions (with `plugin_config`) and calling `grantable_students`. Squad members
already get one row each with the shared `submission_id`, so a per-member button falls out
of the existing row shape.

`teacher_assignment.html`, status cell, next to the rerun/retry links:

```html
{% if (row.submission_id, row.student_id) in grantable %}
<form method="POST" action="/teacher/submissions/{{ row.submission_id }}/grant-quiz-attempt" class="inline">
  <input type="hidden" name="student_id" value="{{ row.student_id }}">
  <button …same classes as rerun…>{{ vocab.teacher.grant_quiz_attempt }}</button>
</form>
{% endif %}
```

`i18n/uk.yml`:

- `teacher.grant_quiz_attempt: Додаткова спроба тесту`
- `quiz.notif_extra_attempt_title: Додаткова спроба тесту`
- `quiz.notif_extra_attempt_body: "Викладач надав вам ще одну спробу тесту з «{title}»."`

## Squads

Per `docs/features/squads.md`, an exhausted member fails the whole squad. The grant is per
member: the teacher grants the member who exhausted; members who already passed keep their
passed attempts and are untouched. The retry draw reuses the stored `squad_quiz_draw`, as
any retry does. The `QUIZ_SENT` self-loop covers the "two members exhausted" case above.

Docs updates: new edge-case row in `docs/features/squads.md`; new route row and
state-diagram lines in `docs/feature_catalog.md`; a line in the unstick list of
`docs/teacher_journey_guide.md`.

## Testing

- **Unit** `tests/unit/test_state_machine.py`: the two legal edges; illegal from
  `COMPLETED` and `AWAITING_TEACHER_REVIEW`.
- **Unit** `tests/unit/test_quiz_grants.py`: `extra_attempts` / `effective_max_attempts`
  on empty, missing and populated metadata (string keys).
- **Functional** `tests/functional/test_teacher_grant_quiz_attempt.py`:
  - solo happy path: exhausted `FAILED` → 303, status `QUIZ_SENT`, metadata `{sid: 1}`,
    attempt rows untouched, audit row, notification row;
  - student can then start a new attempt (303 to a new `/portal/quiz/{id}`), failing it
    returns the submission to `FAILED` with `attempts_left == 0`; passing it completes;
  - assignment detail after grant shows the retry link (`attempts_left > 0`);
  - refusals (409): `COMPLETED`; `FAILED` by teacher reject (no attempts); `FAILED` with
    a passed attempt; not exhausted; no `max_quiz_attempts` in config; wrong `student_id`;
  - other teacher → 403;
  - board renders the button only for the grantable row;
  - squad: exhausted member granted → `QUIZ_SENT`, partner's passed attempt intact; a
    second exhausted member is grantable while already `QUIZ_SENT`.

## Assumptions made in lieu of the approval gate

1. Storage in `source_metadata` rather than a new table/column (request: "decide storage
   yourself").
2. In-app notification only, no email.
3. Button on the assignment board only (not on the review page), matching where
   rerun/retry live.
4. A grant on a squad submission is per member, one member per click.
