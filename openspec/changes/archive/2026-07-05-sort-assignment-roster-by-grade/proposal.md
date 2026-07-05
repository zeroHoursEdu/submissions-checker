## Why

The teacher's per-assignment roster (`GET /teacher/subjects/{id}/assignments/{sa_id}`) already
displays each student's grade (`templates/teacher_assignment.html`'s `col_grade` column), but the
list is ordered only by `Student.full_name` — alphabetically, with no relationship to grading
status at all. A teacher scanning for ungraded or low-scoring work has to read the whole
alphabetical list; there's no way to see "who needs attention" at a glance.

## What Changes

- The roster query orders by `StudentAssignment.grade` ascending (nulls first — ungraded
  students surface at the top, where they need the teacher's attention most), with student name
  as the tiebreaker for students sharing a grade (or all ungraded).

## Capabilities

### New Capabilities
- `teacher-assignment-review`: documents the per-assignment roster page's grade display and sort
  order, since neither was previously covered by any spec.

## Impact

- `src/submissions_checker/api/routes/teacher_portal.py` — `teacher_assignment`'s roster query
  `order_by` changes from `Student.full_name` alone to grade-then-name.
- No template change — the grade column already renders; only row order changes.
- No schema change.
