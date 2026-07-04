## 1. Bug #2 — Admin login redirect

- [x] 1.1 In `src/submissions_checker/api/routes/auth.py`, update `_redirect_by_role` (~line 45-46) to return `/admin` for `UserRole.ADMIN`, `/teacher` for `UserRole.TEACHER`, and `/portal` otherwise.
- [x] 1.2 Add/extend a functional test asserting login as an `ADMIN` user redirects to `/admin`, and that `TEACHER`/student-role redirects are unchanged.

## 2. Bug #3 — ADMIN blocked on 3 subject routes

- [x] 2.1 In `src/submissions_checker/api/routes/teacher_portal.py`'s `delete_subject` (~line 145-154), replace the manual `db.get(Subject, subject_id)` + `owner_id != current_user.user_id` 403 check with `subject = await require_subject_access(db, subject_id, current_user)`.
- [x] 2.2 Apply the same replacement to `provision_test_student` (~line 161-170).
- [x] 2.3 Apply the same replacement to `enter_as_test_student` (~line 219-228).
- [x] 2.4 Add/extend functional tests asserting an ADMIN can successfully call all three routes against a subject owned by a *different* teacher (delete, provision test student, enter as test student), and that a non-owner non-admin teacher still gets 403 on each.

## 3. Bug #4 — Unscoped student roster leak

- [x] 3.1 In `src/submissions_checker/api/routes/teacher_portal.py`'s `GET /students` handler (~line 659-699), add ownership scoping to the query: join through `SubjectsStudents`/`Subject` and filter to `Subject.owner_id == current_user.user_id`, skipping the filter entirely when `current_user.role == UserRole.ADMIN`.
- [x] 3.2 Add/extend a functional test: a teacher owning subject A but not subject B sees only subject A's students; a teacher owning zero subjects sees an empty roster; an ADMIN sees all students.

## 4. Bug #6 — Crashed check script wedges submission

- [x] 4.1 In `src/submissions_checker/workers/tasks/check_tasks.py`, wrap the `run_check()` call (~line 126-129) in `try/except check_core.CheckExecutionError as exc`, calling `_fail_validation(submission, str(exc))` and returning on the except branch.
- [x] 4.2 Add/extend a unit or functional test simulating `check_core.run_check` raising `CheckExecutionError` (e.g. via monkeypatch) and asserting the submission transitions to its failed status with the error message recorded, instead of raising an unhandled exception or leaving the submission in `VALIDATING`.
- [x] 4.3 Add a regression test asserting a *second* check attempt after the failure (simulating retry/resubmission) does not hit `InvalidTransitionError`.

## 5. Bug #7 — Sandbox timeout doesn't kill the container

- [x] 5.1 In `src/submissions_checker/services/docker_sandbox.py`, generate a unique container name (e.g. `f"submission-check-{uuid4().hex}"`) and add it via `--name` to the `docker run` invocation, alongside the existing `--rm`.
- [x] 5.2 In the `asyncio.TimeoutError` handler (~line 84-96), issue `docker kill <name>` (as a subprocess call, best-effort — swallow non-zero exit / errors) in addition to the existing `proc.kill()`.
- [x] 5.3 Add/extend a test asserting that on a simulated timeout, a `docker kill` call is issued with the same container name that was passed to `docker run` (mock/patch the subprocess calls rather than requiring a real hung container).

## 6. Bug #8 — Retired outbox events retry silently to a dead end

- [x] 6.1 In `src/submissions_checker/workers/scheduled/outbox_processor.py`'s dispatch logic (~line 138-194), add an explicit branch before the final unknown-type fallback: if `message.event_type` is `OutboxEventType.PULL`, `.REVIEW`, or `.NOTIFY`, log `outbox_retired_event_type_dropped` (with `event_type`, `message_id`) and mark the message `ERROR` immediately without raising/retrying.
- [x] 6.2 Add/extend a unit test asserting a message with a retired event type is marked `ERROR` after a single processing attempt (not retried), and that a genuinely unknown (non-retired) event type still follows the existing retry-then-fail path unchanged.

## 7. Bug #11 — No sandbox output file-count cap

- [x] 7.1 In `src/submissions_checker/services/docker_sandbox.py`, add a module-level `MAX_OUTPUT_FILES` constant (matching `safe_zip.py`'s naming convention).
- [x] 7.2 In `_read_output_dir` (~line 123-133), stop adding files to the result once `MAX_OUTPUT_FILES` have been collected, silently omitting the rest (same silent-skip style as the existing per-file size cap).
- [x] 7.3 Add/extend a unit test asserting that when `output_dir` contains more than `MAX_OUTPUT_FILES` files, only `MAX_OUTPUT_FILES` are present in the returned dict.

## 8. Docs and verification

- [x] 8.1 Update `docs/known_bugs.md`: mark bugs #2, #3, #4, #6, #7, #8, #11 as ✅ fixed, each with a one-line note on the fix.
- [x] 8.2 Run the full unit + functional test suite and confirm no regressions.
