# Subject tabs rework — design

Date: 2026-09-16. Branch: `worktree-gradebook-integrity-tab` (continues on the
already-implemented gradebook/integrity branch — not yet merged to `main`).

## Goal

Replace the just-built Оцінки tab and the Огляд tab's placement with a
reworked four-tab layout that goes from subject-wide signal to local
administration: **Панель** (four headline stat cards, cheap to load, backed
by a scheduled job) → **Завдання** (task list, now with a per-task
pending-review count) → **Студенти** (a grid of quiz/review sub-marks per
assignment, replacing the single-mark grid) → **Операції** (enrollment —
CSV and a new search-by-email flow — plus the feedback-request button,
moved out of the page header).

This supersedes parts of the prior spec
(`2026-09-16-gradebook-integrity-tab-design.md`): the single-grade grid, the
live-computed stat cards, and the per-attempt integrity table are all
replaced or removed. `fetch_roster_rows`, `RosterRow`, `cell_status`, and the
tab-switcher JS scaffolding survive and are extended, not rewritten.

## Locked semantics (resolved during brainstorming)

- **Середній бал (average mark)** = `100 * Σgrade / Σmax_grade`, summed only
  over (student, assignment) pairs that have a grade — ungraded work is
  excluded from both sums, not counted as zero. A subject with nothing
  graded yet reports `null` (rendered as "—"), not 0%.
- **% зданих (pass %)** = passed pairs / **all** enrolled-student ×
  assignment pairs, where "passed" = `grade >= assignment.min_grade`.
  Ungraded pairs count in the denominator as not-passed. (This replaces the
  prior spec's per-student "passed every assignment" definition — the new
  metric is per-work, not per-student.)
- **На перевірці (pending review)** = same rule as before:
  `grade IS NULL` and a `Submission` exists in any non-terminal status.
  Computed subject-wide for the Панель card, and per-assignment for the
  Завдання tab's per-task count (same predicate, grouped by assignment
  instead of summed).
- **% списувань (cheating %)** = flagged attempts / all finalized quiz
  attempts, subject-wide. "Flagged" reuses `IntegrityRow.flagged` exactly as
  defined on the existing branch (severity present OR duration-anomalous OR
  `other_events > 0`). A subject with zero finalized attempts reports `null`
  ("—"), not 0%.
- **Тест / Огляд sub-marks** (Студенти grid) = `Submission.grade_breakdown`'s
  `quiz_score` and `quality_score` keys (0–100 floats, independent of each
  other and of the final scaled `grade`) on the student's latest submission
  for that assignment. `null` when no `grade_breakdown` exists yet (renders
  "—"). These are **not** rescaled to the assignment's `min_grade`/
  `max_grade` — they display as the raw 0–100 component percentages
  `services.grading.compute_grade` already produces.
- **Разом (total)** = unchanged: sum of the final, scaled `StudentAssignment.grade`
  across assignments — independent of the Тест/Огляд sub-marks, which are
  informational components, not what gets summed.
- The integrity table and the grid's violation-dot indicator are **removed**.
  Per-attempt detail is not surfaced at the subject level anymore; the
  cheating % card is the only subject-level signal. (Per-submission
  violation flags already exist on the task-level assignment page,
  untouched by this rework.)

## Architecture

### New: scheduled, cached subject stats

A new table, written by a new APScheduler job on the same pattern as the
existing `metrics_refresh`/`teacher_digest_processor` jobs
(`core/scheduler.py`, `workers/scheduled/*.py`), read live by the route.
This is a deliberate move away from computing these four numbers on every
page load — the owner is fine with up to ~5 minutes of staleness.

```
subject_gradebook_stats
  subject_id            BIGINT PK, FK -> subjects.id ON DELETE CASCADE
  pending_review_count  INTEGER NOT NULL
  average_mark_pct      DOUBLE PRECISION NULL   -- null = nothing graded yet
  pass_pct              DOUBLE PRECISION NOT NULL
  cheating_pct          DOUBLE PRECISION NULL   -- null = no finalized attempts yet
  computed_at           TIMESTAMPTZ NOT NULL
```

New Alembic migration `0026_subject_gradebook_stats.py`. New model
`SubjectGradebookStats` (`db/models/subject_gradebook_stats.py`), added to
`db/models/__init__.py`.

New job `workers/scheduled/subject_stats_refresh.py::refresh_subject_gradebook_stats()`:
iterates every `ACTIVE` subject, for each one calls `fetch_roster_rows` (for
pending/pass/average) and `fetch_integrity_rows` (for cheating %, counting
`len(rows)` and `sum(r.flagged for r in rows)` — no new query, this function
already excludes TEST students and requires enrollment per the fix already
on this branch), computes the four numbers via a new pure function
`compute_cached_stats(rows, integrity_rows, *, now) -> CachedSubjectStats`
in `gradebook.py`, and upserts one row per subject inside a single
transaction (same `get_session()` + advisory-lock pattern as
`teacher_digest_processor.py`, new lock id distinct from the existing
`7919`/`7927`). Registered in `core/scheduler.py` with a new setting
`subject_stats_refresh_interval: int = 300` (`core/config.py`).

The route (`teacher_subject`) reads the cached row with a plain
`SELECT ... WHERE subject_id = ?` and passes it to the template as
`gradebook_stats` (same context key name, new shape: `CachedSubjectStats`
or `None` if the job hasn't run yet for a brand-new subject — the template
shows "—" / "ще не пораховано" placeholders in that case rather than
crashing or showing a misleading 0).

### Tab restructure

- **Панель** (renamed from the old "Оцінки", now first/default tab): the
  four stat cards only. No grid, no integrity table.
- **Завдання**: unchanged list markup, plus a small pending-review count
  badge per assignment row, computed from the same `fetch_roster_rows`
  result the Студенти tab already needs (grouped by `assignment_id`, no
  extra query). Also fixes the noted low-contrast deadline/points line
  (`text-slate-400` → `text-slate-500` or darker).
- **Студенти**: the new grid. Requires group name and the two
  `grade_breakdown` sub-marks per cell, which `fetch_roster_rows` doesn't
  carry — a new function `fetch_grid_rows(db, subject_id)` in `gradebook.py`
  extends the existing roster query pattern with a `Group` join and a
  `Submission.grade_breakdown` select, kept separate from `fetch_roster_rows`
  so the scheduled job's query stays lean (it doesn't need group name or
  breakdown data). New dataclasses `GridRow`/`GridCell`/`GridColumn` (the
  existing `GradebookRow`/`GradebookCell`/`GradebookColumn`/`GradebookGrid`
  are repurposed in place — same names, extended shape — rather than adding
  parallel types, since nothing outside this feature consumes them).
- **Операції** (renamed from "Огляд", moved last): existing enroll-CSV card
  and owner-only Test Student panel, unchanged, plus:
  - A new search-by-email enroll flow: a debounced (300ms) text input,
    minimum 3 characters before it fires, `GET
    /teacher/subjects/{subject_id}/students/search?q=` (teacher-owner-only,
    `ILIKE` on `Student.email`, limit 10, JSON response) rendering a
    dropdown of matches. Picking one reveals a single "variant" field —
    required (client + server, 422 on missing) only when the subject has at
    least one assignment with `variants_required: true` in its config,
    otherwise omitted — and an Enroll button posting to a new
    `POST /teacher/subjects/{subject_id}/students/enroll-by-search`
    (`student_id`, `variant` form fields). The POST reuses the exact
    enrollment logic `enroll_student` and `import_subject_students` already
    share (`SubjectsStudents` insert if not already enrolled +
    `_ensure_assignment_rows(db, student_id, subject_assignment_ids,
    variant)` + `audit(...)`), so enrollment semantics stay identical
    across all three entry points (existing button-enroll, CSV, and this
    new search flow).
  - The feedback-request button (`Переглянути відгуки` /
    `Запросити відгук` / the no-semester/already-sent disabled states) and
    its result banners (`feedback_sent`, `feedback_error`) move from the
    page header into this tab's content, matching how `enroll_result`/
    `test_student_flash` already live inside a tab rather than page-level.
- `default_tab` logic extends to open **Операції** after a feedback action
  too, not just enroll/test-student: `"operations" if (enroll_result or
  test_student_flash or feedback_sent or feedback_error) else "panel"`.

## Files touched

- `alembic/versions/0026_subject_gradebook_stats.py` — new
- `src/submissions_checker/db/models/subject_gradebook_stats.py` — new
- `src/submissions_checker/db/models/__init__.py` — export the new model
- `src/submissions_checker/services/gradebook.py` — new
  `CachedSubjectStats`, `compute_cached_stats`, `fetch_grid_rows`,
  `GridRow`/`GridCell`/`GridColumn` (or repurposed `Gradebook*` types); old
  `compute_stats`/`build_grid`/`GradebookStats`/`GradebookGrid` either
  removed or repurposed per the above — no dead code left behind
- `src/submissions_checker/workers/scheduled/subject_stats_refresh.py` — new
- `src/submissions_checker/core/scheduler.py` — register the new job
- `src/submissions_checker/core/config.py` — new
  `subject_stats_refresh_interval` setting
- `src/submissions_checker/api/routes/teacher_portal.py` —
  `teacher_subject()` reads the cached stats row instead of computing live;
  two new routes (search, enroll-by-search)
- `templates/teacher_subject.html` — tab rename/reorder, Панель card-only
  content, Завдання per-task badge + contrast fix, Студенти new grid,
  Операції new search-enroll UI + relocated feedback button, `default_tab`
  wiring
- `i18n/uk.yml` — new/renamed vocab keys for all of the above
- Tests: unit tests for `compute_cached_stats`; functional tests for
  `fetch_grid_rows`, the scheduled job (can call
  `refresh_subject_gradebook_stats()` directly against the functional DB,
  same pattern as other scheduled-job tests in this repo if any exist —
  check `tests/integration/test_teacher_digest.py` for the convention),
  the two new routes (search min-length/ILIKE, enroll-by-search variant
  requirement and reused enrollment semantics), and the reworked
  `teacher_subject` route/template tests (replacing/updating the ones from
  the prior spec that no longer apply — integrity table, old grid shape,
  old stat-card semantics).

## Non-goals (unchanged from the prior spec)

No CSV export. No inline editing of marks. Task-level pages
(`teacher_assignment.html` and its route) are untouched.

## Testing

- Unit: `compute_cached_stats` — average-mark sum/sum over graded-only
  pairs (including the "nothing graded → null" case), pass-% over all pairs
  including ungraded, cheating-% over integrity rows including the
  "zero finalized attempts → null" case.
- Functional: the scheduled job populates `subject_gradebook_stats`
  correctly for a fixture subject and is idempotent (running twice updates
  the same row, doesn't duplicate); the route renders "—" placeholders when
  no cached row exists yet; `fetch_grid_rows` returns quiz/review sub-marks
  correctly sourced from `grade_breakdown`, group name, and unchanged
  `Разом` semantics; the search endpoint respects the 3-character minimum
  and matches by email substring; the enroll-by-search endpoint enforces
  the variant requirement exactly when the subject has a
  `variants_required` assignment, and produces identical
  `SubjectsStudents`/`StudentAssignment` state to the existing CSV/button
  enroll paths for the same inputs.
