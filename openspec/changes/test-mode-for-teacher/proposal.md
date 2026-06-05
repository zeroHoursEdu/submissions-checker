## Why

Teachers need a way to verify that subjects and assignments are configured correctly before real students engage with them. Without a test account, teachers must either trust the config blindly or ask a real student to attempt work on their behalf.

## What Changes

- Add a `type` column (enum `REAL` / `TEST`) to both the `students` table and the `groups` table to clearly mark test entities at every level.
- Add a dedicated test group `__TEST__` (with `type = TEST`) seeded by the Alembic migration — no lazy runtime creation needed.
- Add a "Create Test Student" button on the subject detail page (teacher view). If a test student already exists for that subject, display their credentials instead of recreating.
- When a test student is created they are automatically enrolled in the subject.
- Allow teachers to switch into the test student session directly from the subject detail page ("Enter as Test Student").
- Exclude all users / submissions where `type = TEST` from statistics, leaderboards, analytics, and any aggregate queries.

## Capabilities

### New Capabilities

- `test-student-mode`: Per-subject test student provisioning and teacher impersonation — create, view credentials, and enter as the test student account directly from the subject detail page.

### Modified Capabilities

- `subject-management`: Subject detail page gains a "Test Student" panel (create / show credentials / enter as student).

## Impact

- **DB**: `students` and `groups` tables each gain a `type VARCHAR(10) NOT NULL DEFAULT 'REAL'` column (CHECK `REAL`/`TEST`). The `__TEST__` group row is inserted by the migration. New Alembic migration required.
- **Auth / session**: Teacher must be able to "impersonate" the test student — a lightweight session-swap endpoint (`POST /teacher/subjects/{subject_id}/test-student/enter`) that issues a short-lived cookie / token scoped to the test account.
- **Stats & analytics**: Every query that aggregates submissions or students must gain a `WHERE type = 'REAL'` (or equivalent join filter) guard.
- **Student portal**: No behaviour change; test students see the same UI as real students.
- **No breaking changes** to existing real-student flows.
