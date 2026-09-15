# Known Bugs

Compiled from a live end-to-end smoke test (teacher + student flows, quiz, analytics) and a
targeted code audit on 2026-07-04. Each entry gives evidence (file:line) and, where useful,
how it was observed. Severity is a rough call, not a formal triage.

Status legend: 🔴 open · 🟡 open, low impact · ✅ fixed. Update as these get addressed.

---

## 1. 🔴 Plugin-autoloaded subjects get a permanently unclaimable `owner_id = NULL`

**Where:** `src/submissions_checker/services/plugin_loader.py` (`_upsert_subject`, ~line 165)
creates `Subject(...)` on first load with no `owner_id` at all. Every ownership check in
`src/submissions_checker/api/routes/teacher_portal.py` (e.g. lines 152, 168, 226, 291, 825) is
a strict `subject.owner_id != current_user.user_id`, which is `True` (→ 403) for `NULL`
forever, for every teacher, including the one who later re-uploads that subject's config.

**Why it doesn't self-heal:** `ConfigApplyService` (`services/config_apply.py`) only sets
`owner_id` when *creating* a brand-new subject (`_execute_plan`, ~line 374). If the subject
already exists (created by the plugin loader with NULL owner), re-uploading the same config
via `POST /teacher/subjects/apply-config` never claims ownership — `_check_ownership` (line
154) explicitly *allows* the upload through when `owner_id is None`, but nothing then writes
`owner_id`.

**Impact:** any subject present under the `plugins/` directory at container startup (used for
local/dev bootstrapping, e.g. `plugins/e2e_test`) can never be opened, managed, reviewed, or
have a test student created for it by a teacher through the UI — every one of those routes
403s. Reproduced live: fresh DB → app boots → `GET /teacher/subjects/{id}` for the
auto-loaded subject → `403 {"detail":"Not authorized for this subject"}`.

**Workaround used during testing:** manually `UPDATE subjects SET owner_id = <teacher id>
WHERE id = <subject id>` before continuing.

**Update (2026-07-05):** the root cause — the startup plugin-loader scan — has been removed
entirely (`disable-plugin-autoloading`); `plugin_loader.py` no longer exists, and subjects can
only be created via `POST /teacher/subjects/apply-config`, which always sets `owner_id` on
create. New subjects can no longer end up in this state.

Existing `owner_id = NULL` rows are still broken, and re-uploading does **not** self-heal them:
`_execute_plan`'s update branch (`subject is not None`) only calls `_apply_subject_fields`, which
never touches `owner_id` — only the create branch sets it. This bug's effect on pre-existing rows
is unresolved; fixing it needs an explicit backfill (e.g. a one-off `UPDATE` per the workaround
above, or a small follow-up change teaching `_execute_plan` to claim `owner_id` on update when it
is currently `NULL`). Out of scope for `disable-plugin-autoloading`.

---

## 2. ✅ Admin login redirects into a 403

**Where:** `src/submissions_checker/api/routes/auth.py`, login handler picks
`redirect_url = "/teacher" if role == UserRole.TEACHER else "/portal"`. There is no branch for
`ADMIN`, so an admin logging in lands on the student-only `/portal`, which immediately 403s.

**Impact:** cosmetic but real — there is no self-serve admin account creation anywhere in the
app either (confirmed: no route creates an ADMIN user), so this path is presumably only
exercised by whoever seeds an admin directly in the DB, but it's still broken UX whenever it
is exercised.

**Fixed (2026-07-05):** `_redirect_by_role` now returns `/admin` for `UserRole.ADMIN`
(`fix-known-bugs-batch`).

---

## 3. ✅ Ownership check inconsistency lets ADMIN get blocked on a few subject-scoped routes

**Where:** `teacher_portal.py` — `delete_subject` (line 152), `provision_test_student` (line
168), `enter_as_test_student` (line 226) each hand-roll `if subject.owner_id !=
current_user.user_id: raise 403`. Every *other* subject-scoped route in the same file uses the
shared `require_subject_access` helper (`api/authz.py:12-28`), which explicitly admits
`UserRole.ADMIN`. Since `TeacherUser` already lets ADMIN into this whole router, the practical
effect is: an ADMIN can enroll/unenroll students, review submissions, and export grades for
any teacher's subject, but gets 403 trying to delete that subject or provision/enter its test
student — inconsistent with sibling endpoints, and (combined with bug #1) means **nobody**,
not even an admin, can delete or test-drive a plugin-autoloaded subject via the UI.

**Fixed (2026-07-05):** all three routes now call `require_subject_access` instead of
hand-rolling the owner check (`fix-known-bugs-batch`).

---

## 4. ✅ Unscoped global student roster leak

**Where:** `teacher_portal.py:658-698`, `GET /teacher/students`. No subject/ownership `WHERE`
clause at all. Any authenticated TEACHER — even one who owns zero subjects — can view every
student's full name, email, group, username, active flag, and first-login timestamp,
platform-wide. Inconsistent with the strict per-subject ownership model enforced everywhere
else in this file.

**Fixed (2026-07-05):** the query now joins through `SubjectsStudents`/`Subject` and filters to
`Subject.owner_id == current_user.user_id`, skipped for ADMIN (`fix-known-bugs-batch`).

---

## 5. 🔴 `/api/v1/users` endpoints are live but fake

**Where:** `src/submissions_checker/api/routes/users.py:12-38` (`POST /api/v1/users`) and
`:41-66` (`GET /api/v1/users/{user_id}`) return a hardcoded `{"status": "not_implemented"}`
with zero DB interaction, yet the router is mounted in production (`main.py:115`). A publicly
reachable API surface that silently does nothing — looks like a real endpoint from the outside
(200 OK, JSON body) but performs no action.

---

## 6. ✅ A crashed check script permanently wedges the submission (no student/teacher-visible error)

**Where:** `check_core.py:256-269` raises `CheckExecutionError` on a non-zero check-script exit
or missing/invalid `result.json`. `check_tasks.py:126-129` has no try/except around
`run_check()`. The generic catch in `outbox_processor.py` marks the outbox message ERROR and
retries, but the submission was already flipped to `VALIDATING` before the crash and that
commit sticks. On retry, `execute_check_task` calls `transition(submission, "start_validation")`
again — but `state_machine.py` only allows that event from `PENDING`, not `VALIDATING`
(`InvalidTransitionError`), so every retry fails identically until `outbox_max_retries` is
exhausted and retries simply stop. **Net effect:** the submission is stuck in VALIDATING
forever with no error surfaced to the student or teacher. (The standalone CLI runner in
`cli/runner.py:240` handles this case gracefully — only the production worker path doesn't.)

**Reproduced live** while smoke-testing the `pythonBasics` subject: an early version of the
local test harness made `lab4`'s check script crash with an unrelated `NameError`, and the
submission wedged in `VALIDATING` exactly as predicted, with the outbox retrying and failing
identically 5 times (`"No transition for event='start_validation' from
status=<SubmissionStatus.VALIDATING>"`) before giving up. Confirms this is a real, hit-in-
practice failure mode, not just a theoretical read of the code.

**Fixed (2026-07-05):** `execute_check_task` now catches `check_core.CheckExecutionError`
around the `run_check()` call and routes it through `_fail_validation`, so the submission
converges on `VALIDATION_FAILED` with the error recorded instead of raising and getting stuck
mid-transition (`fix-known-bugs-batch`).

---

## 7. ✅ Sandbox timeout kills the CLI wrapper, not the container

**Where:** `docker_sandbox.py:84-96` — on `asyncio.TimeoutError`, calls `proc.kill()` on the
local `docker run` subprocess. `--rm` is honored by the (now-dead) CLI process, not
necessarily the daemon in every failure mode; a hung/slow student script can in principle keep
running inside an orphaned container past the configured timeout, consuming CPU/memory.

**Fixed (2026-07-05):** each sandbox run gets a unique `--name`, and the timeout handler now
issues `docker kill <name>` in addition to killing the local process (`fix-known-bugs-batch`).

---

## 8. ✅ Retired legacy outbox events can wedge silently

**Where:** `outbox_processor.py:162-194` — a stray leftover `PULL`/`REVIEW`/`NOTIFY` outbox
row (the retired GitHub-PR-ingest event types) falls into the "unknown event type" branch,
raises `ValueError`, and is retried up to `outbox_max_retries` with no operator-facing alert
before landing permanently in ERROR.

**Fixed (2026-07-05):** retired event types now get an explicit branch that logs
`outbox_retired_event_type_dropped` and pre-exhausts the retry budget, so they fail once
instead of retrying to exhaustion (`fix-known-bugs-batch`).

---

## 9. 🟡 Quiz-result email notifications fail against the configured Brevo key

**Observed live:** `POST https://api.brevo.com/v3/smtp/email` → `401 Unauthorized` when a quiz
result notification fires. The outbox message ends up in ERROR and retries with backoff. The
`BREVO_API_KEY` in `.env` appears invalid/expired for this environment — not a code bug per se,
but worth flagging since it silently breaks a real notification path.

---

## 10. 🟡 Entire teacher-side "manage subject / assignment / quiz" UI is dead

**Where:** verified by cross-referencing every form `action=` against actual routes:
- `templates/teacher_subject_form.html` posts to `/teacher/subjects/create` and
  `/teacher/subjects/{id}/edit` — neither route exists, and this template is never even
  rendered by any route (`render(..., "teacher_subject_form.html", ...)` appears nowhere in
  `src/`).
- `templates/teacher_assignment_form.html` posts to `.../assignments/create`, `.../edit`,
  `.../delete` — none exist.
- `templates/teacher_quiz_editor.html` posts to `.../quiz/import`, `/quiz/config`,
  `/quiz/questions`, `/quiz/questions/{id}/update`, `/quiz/questions/{id}/delete`,
  `/quiz/export` — none exist. The only route matching `/subjects/{id}/assignments/{sa_id}/quiz`
  anywhere is the **student-facing** one in `student_quiz.py`; the "Manage Quiz" link in
  `templates/teacher_assignment.html` has no teacher-side destination at all.

The only real way to create/update a subject, assignment, or quiz is the config-ZIP upload
flow (`POST /teacher/subjects/apply-config`, see `docs/PLUGIN_AUTHORING.md`). Everything else
in these three templates 404s on submit. (For contrast, `teacher_add_student.html` and the
delete-subject form in `teacher_subject_form.html` DO have working backends.)

**Partially fixed (2026-09-15):** `config-only-subject-management` removed every link into
these templates — the "Manage Quiz", "Edit assignment", "Edit subject", "Add assignment",
"Remove Subject" and "Export CSV" affordances are all gone from `teacher_subject.html` and
`teacher_assignment.html`, so nothing can reach a dead form any more. That change also made
config-only editing a *stated requirement* rather than an accident, in
`openspec/specs/subject-management/spec.md`. What remains open is cosmetic: the orphaned
templates (`teacher_subject_form.html`, `teacher_assignment_form.html`,
`teacher_quiz_editor.html`) are still in the tree and can be deleted whenever convenient.

---

## 11. 🟡 No nested-archive / output-file-count limits

**Where:** `utils/safe_zip.py` enforces a solid 500MB uncompressed cap, 10,000-entry cap, and
rejects absolute paths / traversal / symlinks — good. But there's no recursion guard for a
zip-of-zips (not currently exploitable since nothing auto-recurses into nested archives, but
worth a note for the future), and `docker_sandbox.py:123-133`'s `_read_output_dir` caps each
output file at 1MB but has no cap on the *number* of files a check script can write to
`/output` before they're all read into memory.

**Partially fixed (2026-07-05):** `_read_output_dir` now stops at `MAX_OUTPUT_FILES` (1,000)
(`fix-known-bugs-batch`). The nested-archive recursion guard remains a future-facing note —
not currently exploitable, left open.

---

## 12b. 🔴 "Create Test Student" never assigns variants, so it can't submit to any `variants_required` assignment

**Where:** `teacher_portal.py:159-214` (`provision_test_student`) creates a `StudentAssignment`
row per assignment (line 209) with no `variant` set — it's left `NULL`. For any assignment
with `variants_required: true` in its config (the pythonBasics subject sets this on every one
of its 9 labs), `check_core.resolve_check_plan` (`services/check_core.py:134-137`) returns
`ConfigError("Your variant has not been assigned yet. Contact your teacher to have your
variant set.")` whenever `variant` is `None`. The only place in the whole app that actually
assigns a variant is the CSV enrollment-import flow (`teacher_portal.py:632-648`, the
`variant_<code>` columns), which the test-student feature bypasses entirely.

**Impact:** for any subject where every assignment requires a variant (a common and
recommended setup — see `docs/PLUGIN_AUTHORING.md`), the "Create Test Student" / "Enter as
Test Student" QA feature from the `test-mode-for-teacher` change is dead on arrival: the
teacher can log in as the test student and submit, but every submission immediately fails
validation with "variant not assigned," with no in-UI way to fix it short of a direct DB
write. Reproduced live while testing the `pythonBasics` subject (all 9 labs have
`variants_required: true`).

---

## 12. 🟡 `pythonBasicSubject/` at the submissions-checker repo root is empty / disconnected

The actual course content for the Python-basics subject lives in its own separate repo
(`pythonBasicSubject`, mounted separately during testing) — it is **not** the same as the
identically-named-but-empty `pythonBasicSubject/` folder at the root of this repo, nor
`plugins/pythonBasics/` (both are just auto-generated `CLAUDE.md` stub directories with zero
git history). An archived openspec change (`generate-checks-for-python-basics`, archived
2026-06-19) claims this content was built, but whatever it produced was never committed here.
Not a runtime bug, but likely to confuse anyone who goes looking for it in this repo.

---

*See `docs/missing_features.md` for planned-but-unshipped work and half-built scaffolding.*
