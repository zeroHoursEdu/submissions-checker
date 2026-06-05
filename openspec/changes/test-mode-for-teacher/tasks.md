## 1. Database — Schema & Migration

- [x] 1.1 Add `EntityType` StrEnum (`REAL`, `TEST`) to `src/submissions_checker/db/models/enums.py` (shared by both `students` and `groups`)
- [x] 1.2 Add `type` column (`EntityType`, NOT NULL, default `REAL`) to the `Group` model in `src/submissions_checker/db/models/group.py`
- [x] 1.3 Add `type` column (`EntityType`, NOT NULL, default `REAL`) to the `Student` model in `src/submissions_checker/db/models/student.py`
- [x] 1.4 Create `SubjectTestStudent` model (`subject_test_students` table: id, subject_id UNIQUE FK, student_id FK, plain_password, created_at) in `src/submissions_checker/db/models/subject_test_student.py`
- [x] 1.5 Export `SubjectTestStudent` from `src/submissions_checker/db/models/__init__.py`
- [x] 1.6 Write Alembic migration `0019_test_student.py`: add `groups.type` column + `students.type` column + `subject_test_students` table + INSERT the `__TEST__` group row (`name='__TEST__'`, `type='TEST'`, `description='System group for test students'`)

## 2. Backend — Test Student Provisioning

- [x] 2.1 Implement `POST /teacher/subjects/{subject_id}/test-student` endpoint: check ownership, look up the `__TEST__` group (guaranteed to exist after migration), check if test student already exists, create student+user+enrollment+subject_test_student row if not, redirect with `?test_student=created/existing`
- [x] 2.3 Implement `POST /teacher/subjects/{subject_id}/test-student/enter` endpoint: check ownership, fetch test student user, issue JWT via `create_access_token`, set cookie, redirect to `/portal`

## 3. Statistics Filtering

- [x] 3.1 Add `Student.type == EntityType.REAL` filter to dashboard `enrolled_count` subquery (teacher_portal.py ~line 74)
- [x] 3.2 Add `Student.type == EntityType.REAL` filter to subject detail student list query (teacher_portal.py ~line 150)
- [x] 3.3 Add `Student.type == EntityType.REAL` filter to assignment stats rows query (teacher_portal.py ~line 224)
- [x] 3.4 Add `Student.type == EntityType.REAL` filter to enrollment CSV export query

## 4. Subject Detail Page — Test Student Panel

- [x] 4.1 Update `GET /teacher/subjects/{subject_id}` route to also query `SubjectTestStudent` for this subject and pass `test_student_info` to template context
- [x] 4.2 Add "Test Student" panel to `templates/teacher_subject.html`: show "Create" button when `test_student_info` is None (only for owner); show username, password, and "Enter as Test Student" button when it exists
- [x] 4.3 Add `?test_student=created` / `?test_student=existing` flash message handling in the subject detail template

## 5. Validation & Cleanup

- [x] 5.1 Verify migration runs cleanly against a fresh DB (`alembic upgrade head`)
- [x] 5.2 Smoke-test the full flow: create test student → view credentials → enter as student → submit work → return to teacher → check stats exclude test student
