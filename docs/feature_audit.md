# Feature Audit — 2026-09-17

Whole-repo pass over `src/`, `templates/`, `docs/`, `tests/`, `openspec/`, root clutter.
Every claim below was verified by grep / file read / dev-DB query on this date, not
carried over from `docs/missing_features.md` (2026-07-04), which is now partly stale —
see §D for what it got wrong since.

Three buckets:

- **A. Delete** — dead code, no callers, no user path. Removing it loses nothing.
- **B. Finalize** — feature is half-built: the backend exists but nobody can reach it, or
  the UI exists but the backend never fires, or it works but is undocumented/untested.
- **C. Missing** — things a teacher/student needs that the platform does not offer at all.

Priorities are my call: **P1** do soon · **P2** worth doing · **P3** when convenient.

---

## A. Delete

### A1. GitHub / GitLab ingest leftovers — P1

The PR-ingest pipeline was retired (`retire-github-pr-ingest`, 2026-06-19). What is left:

| Where | What | Action |
|---|---|---|
| `pyproject.toml` | `pygithub>=2.5.0` — zero imports anywhere in `src/` | remove dep, `uv lock` |
| `README.md` lines 3–11, 51, 73, 129, 236, 308 | "GitHub integration", "webhooks", `services/github/`, `GITHUB_WEBHOOK_SECRET`, "SQL-based migrations" | rewrite README (see B12) |
| `main.py:103` | FastAPI `description="…with GitHub integration"` | one-line edit |
| `src/submissions_checker/services/github/` | only a `CLAUDE.md` stub + `__pycache__` | `rm -rf` |
| `services/notifications/templates.py` | `new_submission_template`, `passed_template`, `failed_template` — "Hi @{github_username}" wording, **0 callers** | delete 3 functions + their unit tests |
| `workers/tasks/notification_tasks.py` `execute_new_submission_task` | self-described DEPRECATED no-op; `NEW_SUBMISSION` never enqueued | delete handler + outbox dispatch branch |
| `db/models/student.py` `github_username` | column, unique index, form field in `teacher_add_student.html`, shown as `@handle` in `teacher_assignment.html:105`, `teacher_submission_review.html:39`, `student_select.html:33`; **CSV import never sets it** | drop from form + templates now; drop column in a later migration |
| `db/models/subject.py` `github_repo` | read from `githubRepo` in `config_apply.py` (3 places), never displayed | drop from config schema + column |
| `db/models/enums.py` | `SubmissionSourceType.GITHUB_PR/GITLAB_MR`, `OutboxEventType.PULL/REVIEW/NOTIFY` | keep **only** if prod DB has rows. Dev DB has 2 `GITHUB_PR` rows, both from `0002_dummy_data.py`. Check prod, then one migration: `UPDATE … SET source_type='ZIP_UPLOAD'`, drop enum values, delete `_RETIRED_EVENT_TYPES` branch in `outbox_processor.py` |
| `.env.example` | no GITHUB_* left — OK | — |

### A2. Never-wired service scaffolding — P1

All have `# TODO` bodies raising `NotImplementedError`, and **zero callers** in `src/`:

- `services/user_service.py` (+ `tests/unit/test_user_service.py`)
- `services/testing/runner.py`, `services/testing/result_parser.py` (+ `tests/unit/test_testing_runner.py`, `test_result_parser.py`) — the real pipeline is `services/check_core.py` + `services/docker_sandbox.py`
- `services/submission_checker.py` — returns `(True, "")` unconditionally; name-collides with `check_core.check_submission` and confuses grep

Delete the six files and the three test files. Nothing else references them.

### A3. `/api/v1/users` fake endpoints — P1

`api/routes/users.py`: `POST` and `GET /{id}` return `{"status": "not_implemented"}` with a
200, mounted in prod via `main.py`. Known bug #5. Delete the router, the `include_router`
line, the `api_v1_prefix` setting, and the assertions in
`tests/functional/test_coverage_gaps.py` / `tests/integration/test_api.py` that hit it.

### A4. Orphaned templates — P2

Never passed to `render(...)` anywhere in `src/` (confirmed by grep on every `"*.html"`
literal):

- `templates/teacher_assignment_form.html` — posts to routes that don't exist (known bug #10)
- `templates/teacher_quiz_editor.html` — same
- `templates/student_select.html` — pre-auth student picker from the no-login era

Delete all three. Also drop the vocab keys they alone use (`vocab.teacher.github_username_label`, `col_github`, quiz-editor strings) from `i18n/uk.yml`.

### A5. Legacy submission statuses — P2

`SubmissionStatus.PROCESSING / REVIEWING / CHECKING / CHECK_FAILED / WAITING_FOR_TEACHER_REVIEW`,
the `start_check`, `check_passed_*`, `teacher_approve_quiz/done` transitions in
`core/state_machine.py`, and the matching `{% elif status == "CHECKING" %}` branches in
`templates/teacher_assignment.html` (lines 23–43, 109) and `templates/assignments.html`
(23–31). Dev DB has **zero** rows in any legacy status. If prod is the same, delete in one
change: migration to drop enum values, remove transitions, remove template branches.

### A6. Unused settings — P3

`core/config.py`: `ai_temperature`, `base_url` (not `app_base_url`), `api_v1_prefix`
have no readers. `.env.example` still lists `AI_TEMPERATURE`. Remove.

### A7. Stale docs — P2

- `docs/analytics.md` — describes `/teacher/analytics` pages removed in `e9611a2` ("Remove the in-app analytics pages"). Delete.
- `docs/statuses.md`, `docs/jobs.md` — self-marked "DEPRECATED / HISTORICAL". Delete; git history keeps them.
- `docs/feature_catalog.md` §8 Analytics — whole section describes removed routes. §4 claims the review page shows "AI review" — it does not (see B1). §1 lists `/api/v1/users`. Fix after A3/B1.
- `docs/missing_features.md` — see §D; either fold into this file or delete.
- `scripts/quiz_form.gs` — Google Apps Script for the retired Google-Forms quiz pipeline (`docs/jobs.md` era). Delete.

### A8. Openspec housekeeping — P3

`openspec/changes/` still holds:

- **Empty dirs** (only an auto-generated `CLAUDE.md`): `fix-submission-extract-dir-permissions`, `generate-checks-for-python-basics`, `harden-anticheat-keyboard-shortcuts`, `notify-student-on-violation`, `retire-github-pr-ingest`, `subject-config-apply`. `rm -rf`.
- **100 % done, never archived**: `config-only-subject-management` (31/31), `gather-feedbacks` (22/22), `multilanguage-i18n-support` (28/28), `rebrand-and-hide-demo-credentials` (13/13), `test-mode-for-teacher` (17/17). Run `/opsx:archive` on each.
- `prod-deployment` 57/61 — check the 4 open tasks, then archive.

### A9. Root-level clutter (all untracked, none affects the build) — P3

| Path | What it is |
|---|---|
| `pendingPosts/` (21 files) | LinkedIn/Telegram/Bluesky blog drafts about Spring/retries — unrelated to this repo |
| `networking-backend/`, `outlines/`, `rawThoughts/`, `memory/`, `migrations/`, `services/`, `src/components/TextProcessor/` | each contains only an auto-generated `CLAUDE.md` |
| `cppBasicSubject/`, `pythonBasicSubject/` | empty shells; real subjects live in their own repos (`distributedBasics` etc.) |
| `htmlcov/`, `coverage.xml` | coverage output |
| `ssh/` | now gitignored (uncommitted `.gitignore` change) — fine |
| ~150 six-line `CLAUDE.md` stubs in every directory | claude-mem auto-generated; `CLAUDE.md` is globally gitignored (`.gitignore:209`) so they never ship, but they are noise in every `find`/`ls`. `find . -name CLAUDE.md -size -400c -not -path './.claude/*' -delete` clears them |

Move `pendingPosts/` out of the repo; delete the rest.

---

## B. Finalize

### B1. AI review — verdict is computed and then hidden — P1

`workers/tasks/review_tasks.py` + `services/ai/provider.py` are **fully implemented**
(OpenAI JSON-mode or Anthropic structured output; `missing_features.md` is wrong on this).
The verdict `{cheating, ai_generated, code_mark, comment, provider, model}` lands in
`submission.ai_review`. Then:

- `services/grading.py:137` reads `code_mark` for the grade — the only consumer.
- **The teacher never sees the verdict.** `teacher_submission_review.html` shows tests +
  code, never the cheating/AI-generated flags, confidences, reasons or the comment.
  `feature_catalog.md` claims otherwise. (The student *does* get `comment` on
  `assignment_detail.html`, but only when the assignment sets
  `ai_review.show_comment_to_student: true` — undocumented.)
- `collect_lab_data()` only reads `.py/.md/.txt`. A Java or C++ subject (both exist —
  `javaProgramming2`, `cppBasicSubject`) sends `"# No code found"` to the model and gets a
  verdict on nothing.
- `ai_review` config block (`cheating_threshold`, `ai_generated_threshold`) is not in
  `docs/PLUGIN_AUTHORING.md` at all.
- The gradebook integrity tab shows quiz violations only; AI flags are absent.

**Do:** (1) render verdict on `teacher_submission_review.html` (flags + confidence + reason
+ comment) and a flag dot on the assignment board; (2) document
`show_comment_to_student`; (3) take the extension allow-list from the subject config
(`ai_review.source_extensions`, default by language) instead of hard-coding Python;
(4) document the block; (5) add AI flags to the integrity tab.

### B2. Camera proctoring ("AI detection using camera") — P1

Client side in `templates/_quiz_anticheat.html` (lines 170–372) is **complete**: MediaPipe
FaceLandmarker gives face-count and head-pose (`camera_face_absent`,
`camera_multiple_faces`, `camera_looking_away`), optional coco-ssd gives
`camera_phone_detected`, all through the same `/event` rule engine, with evidence frames
posted to `/snapshot` and served back via the authenticated
`/teacher/proctoring/snapshots/{id}`. What is unfinished:

1. **Zero documentation.** `docs/PLUGIN_AUTHORING.md` and `docs/anti-cheat.md` never mention
   `camera`, `detectors`, `require_camera`, `on_no_camera`, `capture_snapshots`,
   `snapshot_on`, `sustain_seconds`, `yaw_deg`, `pitch_deg`, `min_confidence`,
   `sample_seconds`. A teacher cannot turn it on without reading the JS.
2. **Runtime deps come from third-party CDNs at exam time**: `cdn.jsdelivr.net`
   (MediaPipe wasm + JS), `storage.googleapis.com` (face model), `esm.sh` (tfjs + coco-ssd,
   ~10 MB). Any of them down → detection silently off, quiz proceeds unproctored. No CSP.
   Vendor the assets under `static/` (they are static files) and pin them.
3. **Teacher evidence view is thin**: only thumbnails per student on the assignment board.
   No per-attempt timeline (event → frame → timestamp), nothing on the submission review
   page or the integrity tab.
4. **Test coverage**: `test_proctoring_snapshot_access.py` and
   `test_student_quiz_proctoring.py` cover the snapshot route and consent gate; no test
   asserts that `camera_*` events pass through the rule engine or that
   `snapshot_on` gating works.
5. **Phone detector**: coco-ssd on a 320×240 frame, `'cell phone' || 'book'`, is heavy and
   noisy. Decide keep-or-drop; if kept, document the false-positive rate honestly in
   `anti-cheat.md` §"Limitations".

### B3. Deadline reminders — backend only, never fires — P2

`OutboxEventType.DEADLINE_REMINDER`, `execute_deadline_reminder_task`,
`deadline_reminder_template`, the outbox dispatch branch, and the `SUBMISSION_CHECKED`-style
email path all exist. **Nothing enqueues the event**: no scheduled job in
`core/scheduler.py` (only `outbox_processor`, `teacher_digest_processor`,
`metrics_refresh`, `subject_stats_refresh`). Either add a daily `deadline_reminder` job
(StudentAssignments with `deadline` in N days and no submission → enqueue once, dedup by
`(sa_id, deadline)`), or delete the four pieces. The former is a real student-facing win.

### B4. i18n is single-language — P2

Only `i18n/uk.yml` exists; `en.yml` was never committed. `base.html` hides the selector
when `available_languages | length <= 1`, so `/set-language` and the whole
`AVAILABLE_LANGUAGES` machinery are unreachable. `feature_catalog.md` says
"English / Ukrainian". Either ship `en.yml` (the loader already supports it) or delete the
route + selector and keep only the vocab loader.

### B5. Endpoints with no UI — P2

Deliberately unlinked by `config-only-subject-management`, still live and authorized:

- `GET /teacher/subjects/{id}/export.csv` — grades export. Worth a button in the Операції
  tab; teachers will want it at semester end.
- `POST /teacher/subjects/{id}/delete` — soft delete. Either a button behind a confirm in
  Операції, or remove the route.
- (`feedback/export.csv` is fine — linked from `teacher_feedback_view.html`.)

### B6. `SHORT_ANSWER` question type is a dead end — P2

Answers are stored, `is_correct` is `None`, nothing scores them, no teacher UI grades them,
`max_score` still counts them (so a quiz with one short-answer question can never reach
100 %). Either build a manual-grading panel (teacher enters points per answer, re-runs
`finalize_grade`) or reject the type in `config_apply` validation and drop it from docs.

### B7. Semesters have no management surface — P3

`0016_add_semesters.py` seeds Feb–Jun / Sep–Jan rows through Fall 2035. No route creates,
edits, or lists them. Course feedback (`/feedback/request`) hard-fails with
`no_active_semester` outside those windows (e.g. a summer term, or July). Smallest fix: an
admin page listing semesters with add/edit. Alternative: drop the semester FK and key
feedback requests by `(subject_id, year)`.

### B8. Known bugs still open (from `docs/known_bugs.md`) — P2

- **#1** `owner_id = NULL` subjects never self-heal: `config_apply._execute_plan` update
  branch still never writes `owner_id`. One-line fix: claim ownership on update when
  currently NULL.
- **#13** `common_check == variant_check` runs the same script twice, recurred in two
  subjects. Guard in `check_core.resolve_check_plan` still absent.
- **#16** re-applying an older config ZIP is refused (dedup against all versions). Needs
  the migration dropping `uq_subject_plugin_configs_subject_hash`.
- **#11** nested-archive recursion note — leave.

### B9. Air-raid pause has no reaper — P3

An attempt paused and never resumed stays `IN_PROGRESS` forever (documented in
`feature_catalog.md`). Add a scheduled job that closes attempts paused > N hours as
`TIMED_OUT`, or accept and document as policy.

### B10. Stuck-state recovery has no operator path — P2

- `AI_REVIEW_FAILED` after `outbox_max_retries` (5): the outbox row goes `ERROR`, the
  submission stays `AI_REVIEW_FAILED`, and no route lets a teacher retry or skip to manual
  review. Only a DB edit unsticks it.
- Same for `VALIDATING`/`TESTING` if the worker dies mid-task (the crash path is handled,
  the process-kill path is not).

A "Retry / Send to teacher review" button on the submission review page, allowed from
`AI_REVIEW_FAILED` and any non-terminal status older than X minutes, closes this.

### B11. Migrations carry dev seed data — P3

`0002_dummy_data.py` and `0007_quiz_dummy_data.py` insert rows when
`ENVIRONMENT=development`. It works (gated), but they run on every fresh dev DB and the
seed includes `GITHUB_PR` submissions (A1). Consider moving the seed to
`scripts/seed_dev.py` and making the two migrations no-ops.

### B12. README is a description of a different product — P1

Whole file describes GitHub webhooks, PR comments, SQL-file migrations, and a
`services/github/` tree. Replace with: what it is (ZIP upload → sandboxed checks → optional
AI/teacher/quiz review), how to run (`make up`, `make test`), and links to
`docs/feature_catalog.md`, `docs/PLUGIN_AUTHORING.md`, `docs/deployment.md`,
`docs/observability.md`.

---

## C. Missing

Ordered by how often a teacher or student would hit the gap.

1. **Password change for a logged-in user.** Only forgot/reset-by-email exists. Students
   get generated credentials by email and have no way to set their own password without
   the reset dance. `GET/POST /auth/change-password` (old + new twice). P1.
2. **Deadline reminder emails** — see B3. P2.
3. **Show the AI flags and comment to the teacher** — see B1. P1.
4. **`tests_then_teacher_then_quiz` review mode** (or `send_quiz_after_teacher_approval`).
   Today a quiz configured alongside `tests_then_teacher` only fires if the teacher
   remembers to pick "send quiz" on approval. Carried over from `missing_features.md`;
   still true — `_advance_after_tests` in `check_tasks.py` dispatches on one mode. P2.
5. **Teacher-side retry / unstick controls** — see B10. P2.
6. **Manual grading for `SHORT_ANSWER`** — see B6. P2.
7. **Bulk actions on the assignment board**: approve/reject several submissions, resend
   credentials to a whole group, re-run checks for an assignment after a config fix.
   Everything today is one row at a time. P2.
8. **Cross-student similarity report.** Similarity is computed at upload against every
   other student's ZIP for the same assignment (`student_portal.py:478–503`) and shown as
   one number per row. There is no "who matches whom" view, no threshold flag, no link to
   the integrity tab. P2.
9. **Subject archive / delete in the UI** — see B5. P3.
10. **Semester admin** — see B7. P3.
11. **Rate limiting and CSRF** on `POST /auth/login`, `/auth/forgot-password`, and every
    form (deferred in the 2026-06-18 security pass; still absent). P1 for login
    throttling, P2 for CSRF given the strict-same-site cookie.
12. **Config validation for the two recurring authoring mistakes**: identical
    common/variant check script (B8 #13), and a `quiz:` block under a review mode that
    never sends it (item 4). Fail the upload with a clear message. P2.
13. **Abandoned-attempt reaper** — see B9. P3.

---

## D. Corrections to `docs/missing_features.md` (2026-07-04)

| Item there | Status now |
|---|---|
| "AI-assisted code review — service doesn't exist" | **Wrong.** Implemented 2026-07-07 (`add-ai-review`): `services/ai/provider.py`, `review_tasks.py`, 5 `test_ai_review_*` tests pass. What's missing is the UI (B1). |
| "Dead service scaffolding" (`user_service`, `testing/*`, `submission_checker`) | Still true — A2. |
| "Teacher UI has no way to create/edit subject/assignment/quiz by hand" | Resolved by decision: config-ZIP is the stated requirement (`openspec/specs/subject-management`). Only the orphan templates remain — A4. |
| "Teacher-scoped analytics don't exist" | Moot: analytics pages were removed (`e9611a2`); Grafana + the subject Панель tab replace them. |
| "Openspec housekeeping" | Still true, list grown — A8. |
| "`pythonBasicSubject` lives outside this repo" | Still true; the in-repo shell is untracked clutter — A9. |
| "No combined teacher+quiz review mode" | Still true — C4. |

Recommend deleting `missing_features.md` once this file is triaged, so there is one list.
