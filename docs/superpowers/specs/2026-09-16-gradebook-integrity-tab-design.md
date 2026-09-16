# Subject page tabs: gradebook + integrity — design

Date: 2026-09-16. Branch: `gradebook-integrity-tab` (new branch to be cut for this work).

## Goal

`/teacher/subjects/{id}` currently stacks enroll-CSV card → student list → task list →
(owner-only) test-student panel on one screen, with no way to see grades across the
whole subject at a glance. Restructure into tabs and add a read-only gradebook
(per-assignment marks grid + subject-wide stats) and an integrity/violations view for
quiz anti-cheat data, so a teacher can scan a class's standing and catch suspicious
quiz attempts without opening every submission individually.

Also fixes a display bug: enrolled-student rows render `@None` in place of a missing
GitHub username, and the design takes the opportunity to also guard the (currently
unreachable, but not enforced by any check the template can see) case of a missing
group name.

Non-goals: no CSV export, no inline mark editing, no changes to the existing
feedback-request button's behavior.

## Locked semantics (resolved during brainstorming)

- **"Passed" (assignment level)** = `StudentAssignment.grade IS NOT NULL` — any mark
  counts, independent of its value. This is a deliberate simplification the subject
  owner chose over gating on `min_grade`; revisit only if asked.
- **% зданих** = % of enrolled real students whose grade is non-null on *every*
  assignment in the subject. A subject with zero assignments reports 0% (not 100% —
  avoids a vacuously "green" empty subject).
- **Середній бал** = average of all non-null `StudentAssignment.grade` values for the
  subject's assignments.
- **Прострочені** = count of (student, assignment) pairs where
  `assignment.deadline < now()` AND `grade IS NULL` — counted whether or not the
  student ever submitted. Missing work is exactly what this card should surface.
- **На перевірці** = count of (student, assignment) pairs with `grade IS NULL` where a
  `Submission` exists in any non-terminal status (i.e. not `COMPLETED`/`FAILED`).
- **Grid cell color** is a different, richer signal than "passed" above — it reflects
  grade quality, not mere presence:
  - no `Submission` row at all → not-submitted (muted `—`)
  - a `Submission` exists, non-terminal status, `grade IS NULL` → pending (warning)
  - `grade IS NOT NULL` and `grade >= assignment.min_grade` → passed (success)
  - `grade IS NOT NULL` and `grade < assignment.min_grade` → failed (danger)

## Architecture

No new routes, no AJAX/htmx (none exists in this codebase — pages are single-request
Jinja2 renders with vanilla-JS interactivity). Everything is computed once, server-side,
in the existing `GET /teacher/subjects/{subject_id}` handler
(`teacher_portal.py:teacher_subject`), and handed to the template alongside today's
`students`/`assignments`.

```
teacher_subject() [teacher_portal.py]
  ├─ existing: students, assignments, feedback_request, test_student_info, enroll_result
  └─ new: services/gradebook.py
       ├─ compute_stats(db, subject_id)      -> GradebookStats
       ├─ build_grid(db, subject_id)         -> GradebookGrid (rows × assignment cells)
       └─ list_integrity_rows(db, subject_id) -> list[IntegrityRow]
            (reuses per-quiz median duration computed once, not per row)
```

`services/gradebook.py` is pure query/aggregation — it returns plain dataclasses /
dicts, no ORM objects leak past it into the template context (matches how
`teacher_assignment` already passes `rows` as plain dicts, not `Submission` instances).

### Query shape

All three functions share one base dataset: enrolled real students × subject
assignments × latest relevant `Submission`/`StudentAssignment` per pair. Built as one
SQL join (students × subjects_assignments × students_assignments LEFT JOIN
submissions), loaded once, then the three functions fold over the same in-memory rows
rather than issuing three separate round-trips — the "don't recompute per row on the
client" requirement extends server-side too: compute once, derive three views.

Quiz violations require a second query: `QuizAttempt` joined through
`Submission.students_assignment_id`, filtered to the subject's assignments. Only
assignments with `review_mode == "tests_then_quiz"` can have attempts; other
assignments simply produce no integrity rows. When a (student, assignment) pair has
multiple `QuizAttempt`s (retries), the most recent by `started_at` is the one shown —
this is what the gradebook cell's violation indicator also points at.

Median duration is computed **per assignment**, across all of that assignment's
finalized attempts (`submitted_at IS NOT NULL`), using
`submitted_at - started_at - paused_seconds` (air-raid pauses excluded, matching how
the timing code elsewhere already treats `paused_seconds` as not-real-elapsed-time).
Computed once per assignment, not once per attempt.

### Severity computation

Pure function `severity_for(violations: dict, tab_switch: int, window_blur: int,
duration_ratio: float | None) -> Literal["high", "medium", None]`, unit-testable in
isolation:

```
if violations.get("_force_fail") or tab_switch + window_blur >= 3: "high"
elif 1 <= tab_switch + window_blur <= 2: "medium"
else: None
```

Duration anomaly (`duration < 0.3 * median`) is surfaced as its own flag/column on the
row — it does **not** currently escalate severity per the spec's rule list (only
`_force_fail` and switch/blur counts do). The row is still included in "flagged only"
filtering when the duration flag is set even if severity is `None`, since a teacher
asked to see anomalies, not just the two named severities. Any `_`-prefixed key other
than `_force_fail` (e.g. `_time_penalty_seconds`) is internal bookkeeping and is never
surfaced as a named violation type.

## Template / tabs

Plain Tailwind, vanilla JS — four `<button>`s in a tab bar under the `<h1>`, four
`<div>` panels toggled by `hidden` class + `aria-selected`. No framework needed for
four static panels.

- **Огляд**: enroll-CSV card (unchanged) + the owner-only Test Student panel (moved
  here from its current stacked position — both are one-time setup tooling, not daily
  use).
- **Студенти**: today's enrolled-students list, unchanged except the bug fix below.
- **Завдання**: today's assignment list, unchanged.
- **Оцінки**: stat cards (3a) + grid (3b) + integrity table (4), new.

Initial active tab is computed server-side and passed as `default_tab`: **Огляд** when
`enroll_result` or `test_student_flash` is present (so the result of the action the
user just took is visible without an extra click), else **Студенти**. The
`feedback_sent`/`feedback_error` banners stay page-level, above the tab bar — they're
tied to the header button, which lives outside tab content per the task's instruction.
`enroll_result`/`test_student_flash` banners move inside the Огляд panel (they're
specific to actions taken there).

## Gradebook grid (3b)

- Sticky first column (student name, `position: sticky; left: 0`), horizontal
  scroll container (`overflow-x-auto`) around the assignment columns, pinned "Разом"
  column on the right (`position: sticky; right: 0`) — both sticky columns need a
  solid background (not transparent) so scrolled content doesn't show through.
- Cell content: grade value or `—`; status color per the "Grid cell color" rule above,
  reusing the badge-dot visual language already established in
  `teacher_assignment.html`'s `status_badge` macro (small colored dot + tinted
  background) rather than inventing a new visual style.
- "Разом" = sum of non-null grades across the row; `—` if none graded yet.
- Violation indicator: when a cell's submission has a `QuizAttempt` with any non-empty
  `violations` (any key, or `_force_fail`), render a small dot in the cell corner with
  a native `title` tooltip (hover, zero JS) summarizing counts. Clicking the cell
  scrolls to and highlights the matching row in the integrity table below via a small
  JS handler (`id="integrity-row-{student_id}-{assignment_id}"` + `scrollIntoView` +
  a brief highlight class) — no separate modal/detail view, the integrity table *is*
  the detail view.

## Integrity table (4)

One row per finalized `QuizAttempt` on a `tests_then_quiz` assignment — the table is
the full roster of quiz attempts, not pre-filtered to anything suspicious. Columns:
student, assignment/quiz name, tab_switch, window_blur, duration vs median (e.g.
"42s / 210s median" with the anomaly flag styled distinctly when < 30% of median),
severity badge (High/Medium/—). A checkbox above the table ("show only flagged")
filters client-side by toggling `hidden` on rows where severity is High/Medium or the
duration-anomaly flag is set — the severity is already computed server-side per row,
so this is pure DOM filtering, no recomputation.

## Files touched

- `templates/teacher_subject.html` — tabs, moved sections, new gradebook/integrity
  markup, bug fix
- `src/submissions_checker/api/routes/teacher_portal.py` — `teacher_subject()` gains
  the three new context values + `default_tab`
- `src/submissions_checker/services/gradebook.py` — new
- `i18n/uk.yml` — new `teacher.*` vocab keys for tab labels, stat-card labels, grid/
  integrity table headers
- Tests: unit tests for `severity_for` and the median/ratio math in isolation;
  functional test(s) extending the existing `teacher_subject` coverage for the new
  context values and the `@None` fix

## Testing

- Unit: `severity_for` truth table (force_fail, 0/1/2/3+ combined counts); median
  duration computation with even/odd attempt counts and a single-attempt edge case.
- Functional: `GET /teacher/subjects/{id}` includes correct stat numbers for a fixture
  subject with a mix of graded/ungraded/overdue/pending submissions; grid cell status
  matches expected per the four-way rule; the integrity table lists one row per
  finalized `QuizAttempt` on a `tests_then_quiz` assignment (severity `—` included —
  the table is a full roster, not a pre-filtered one) and excludes non-quiz
  assignments entirely; the "flagged only" filter keeps rows where severity is High/
  Medium *or* the duration-anomaly flag is set; `@None` no longer renders for a
  student with `github_username = None`.
