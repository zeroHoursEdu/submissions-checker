## Context

`teacher_assignment`'s roster query (`teacher_portal.py:390`) orders solely by
`Student.full_name`. `StudentAssignment.grade` (nullable `Integer`) is already selected and
already rendered in the template — display isn't the gap, order is.

## Goals / Non-Goals

**Goals:** make the row order reflect what a teacher actually wants to scan for — work that
needs grading or attention — instead of pure alphabetical.

**Non-Goals:** interactive/clickable column-header sorting (JS-driven re-sort in the browser).
The ask is a sensible default order, not a full sortable-table UI component.

## Decisions

**Order by grade ascending, nulls first, name as tiebreaker.** Nulls-first surfaces ungraded
submissions at the top — the students most likely to need the teacher's attention right now.
Ascending (not descending) among graded rows puts the lowest scores next, which is the more
common "who's struggling" scan a teacher does after clearing the ungraded pile. This is a
judgment call (grade-descending "best first" is an equally defensible reading of "sorted"); it's
implemented as a single `order_by` clause, trivial to flip if the actual usage pattern disagrees.

**SQLAlchemy's `nullslast()`/`nullsfirst()` helper, not a `CASE WHEN`.** Cleaner and
database-portable within Postgres (the only backend this app targets).

## Risks / Trade-offs

None significant — a single `ORDER BY` clause change on an already-small per-assignment roster
query (bounded by enrollment size), no new indexes needed at this scale.
