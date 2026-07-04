## Why

`docs/known_bugs.md` documents seven confirmed, live bugs across authorization, submission
reliability, and sandbox resource limits — three are user-facing correctness/privacy bugs (admin
login redirect, ADMIN blocked on some subject routes, a global unscoped student roster leak), one
is a reproduced-in-practice reliability bug (a crashed check script permanently wedges a
submission with no visible error to student or teacher), and three are hardening gaps (sandbox
timeout not killing the container, retired outbox event types retrying to a silent dead end, no
cap on sandbox output file count). All seven are independently scoped, low-risk fixes to existing
code paths — no new features, no schema changes.

## What Changes

- **Admin login redirect (bug #2):** `auth.py`'s post-login redirect adds a branch for
  `UserRole.ADMIN` → `/admin`, instead of falling through to the student `/portal` (which then
  403s).
- **ADMIN blocked on 3 subject routes (bug #3):** `delete_subject`, `provision_test_student`, and
  `enter_as_test_student` in `teacher_portal.py` replace their hand-rolled
  `owner_id != current_user.user_id` checks with the shared `require_subject_access` helper
  (already used by every other subject-scoped route in the file), which admits ADMIN.
- **Unscoped global student roster leak (bug #4):** `GET /teacher/students` adds ownership
  scoping (owned subjects' enrolled students only, ADMIN sees all), matching the ownership model
  enforced everywhere else in `teacher_portal.py`.
- **Crashed check script wedges submission forever (bug #6):** `check_tasks.py` wraps
  `check_core.run_check()` in a try/except for `CheckExecutionError`, routing a crash to the
  existing `_fail_validation()` helper instead of leaving the submission stuck in `VALIDATING`
  with no valid outbound transition.
- **Sandbox timeout kills the CLI wrapper, not the container (bug #7):** `docker_sandbox.py`
  assigns each sandbox run a unique `--name`, and the `TimeoutError` handler issues `docker kill
  <name>` in addition to killing the local `docker run` process, so a hung script's container
  doesn't outlive the timeout.
- **Retired outbox events retry to a silent dead end (bug #8):** `outbox_processor.py`'s dispatch
  recognizes the retired `PULL`/`REVIEW`/`NOTIFY` event types explicitly and marks them `ERROR`
  immediately with a clear log message, instead of retrying `outbox_max_retries` times against an
  unknown-type `ValueError` before going silent.
- **No sandbox output file-count cap (bug #11):** `docker_sandbox.py`'s `_read_output_dir` adds a
  `MAX_OUTPUT_FILES` cap (matching `safe_zip.py`'s named-constant convention), stopping once
  exceeded instead of reading an unbounded number of files into memory.

## Capabilities

### New Capabilities
- `outbox-reliability`: defines that outbox event dispatch fails fast and visibly for
  unrecognized/retired event types, instead of retrying silently to exhaustion.

### Modified Capabilities
- `auth-security`: adds a role-correct post-login redirect requirement (ADMIN → `/admin`).
- `subject-authorization`: closes the ADMIN-bypass gap on 3 specific routes for the existing
  "owner or ADMIN" requirement, and adds a new requirement scoping the teacher student roster to
  owned subjects.
- `check-runner`: adds requirements that a crashed check script fails the submission visibly
  (not silently wedges it), that a sandbox timeout terminates the actual container, and that
  sandbox output reading is bounded by file count as well as per-file size.

## Impact

- `src/submissions_checker/api/routes/auth.py` — redirect branch for `UserRole.ADMIN`.
- `src/submissions_checker/api/routes/teacher_portal.py` — 3 routes switch to
  `require_subject_access`; `GET /students` gains ownership scoping.
- `src/submissions_checker/workers/tasks/check_tasks.py` — try/except around `run_check()`.
- `src/submissions_checker/services/docker_sandbox.py` — named containers + `docker kill`
  fallback on timeout; `MAX_OUTPUT_FILES` cap in `_read_output_dir`.
- `src/submissions_checker/workers/scheduled/outbox_processor.py` — explicit handling for
  retired event types.
- `docs/known_bugs.md` — mark bugs #2, #3, #4, #6, #7, #8, #11 as ✅ fixed.
- No DB schema, API contract, or config changes. No new dependencies.
