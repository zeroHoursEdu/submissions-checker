## 1. Route

- [x] 1.1 In `src/submissions_checker/api/routes/teacher_portal.py`, add `nullsfirst` to the `sqlalchemy` import.
- [x] 1.2 In `teacher_assignment`'s roster query (~line 390), change `.order_by(Student.full_name)` to `.order_by(nullsfirst(StudentAssignment.grade.asc()), Student.full_name)`.

## 2. Tests

- [x] 2.1 Add/extend a functional test seeding 3+ students with a mix of ungraded and graded (varying scores) `StudentAssignment` rows for the same assignment, asserting the rendered order matches: ungraded first (alpha among ties), then ascending grade (alpha among ties).
- [x] 2.2 Run the functional test suite for teacher_assignment/portal pages to confirm no regression.
