# teacher-assignment-review

## Purpose

Defines the teacher-facing per-assignment roster page's grade display and sort order — how
grading status is shown and how students are ordered so a teacher can quickly find who needs
attention.

## Requirements

### Requirement: Per-assignment roster shows each student's grade
The teacher-facing per-assignment roster SHALL display each enrolled student's current grade for
that assignment alongside the assignment's maximum grade, or an ungraded indicator when no grade
has been recorded.

#### Scenario: Graded student shows grade
- **WHEN** a student has `StudentAssignment.grade` set for the assignment
- **THEN** the roster row shows that grade alongside the assignment's max grade

#### Scenario: Ungraded student shows no grade value
- **WHEN** a student has no grade recorded for the assignment
- **THEN** the roster row shows an ungraded indicator, not a blank or zero value

### Requirement: Per-assignment roster is sorted by grade, ungraded first
The teacher-facing per-assignment roster SHALL be ordered by grade ascending, with ungraded
students sorted first, and student name as the tiebreaker among students sharing a grade (or all
ungraded).

#### Scenario: Ungraded students appear before graded ones
- **WHEN** the roster contains both ungraded and graded students
- **THEN** all ungraded students appear before any graded student in the rendered order

#### Scenario: Graded students ordered lowest-first
- **WHEN** the roster contains students with grades 60, 90, and 75
- **THEN** they appear in the order 60, 75, 90

#### Scenario: Ties broken by student name
- **WHEN** two students share the same grade (or are both ungraded)
- **THEN** they appear in alphabetical order by full name relative to each other
