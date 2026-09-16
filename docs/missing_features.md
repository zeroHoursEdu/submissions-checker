# Missing / Incomplete Features

Compiled 2026-07-04 from a code audit (dead scaffolding, open `openspec/changes/`, and
documented-but-unwired UI). This is a working list — fill in `Priority` / `Status` / `Notes`
yourself as you triage.

Priority: leave blank, or use `P0` (blocking) / `P1` (soon) / `P2` (nice to have) / `Won't fix`.
Status: leave blank, or use `Planned` / `In progress` / `Done` / `Wontfix`.

---

## Teacher-facing UI has no way to create/edit a subject, assignment, or quiz by hand

| Priority | Status | Notes |
|---|---|---|
| | | |

The only working path to create or change a subject/assignment/quiz is uploading a
`config.yml`-based ZIP (`POST /teacher/subjects/apply-config`). The dashboard templates
(`teacher_subject_form.html`, `teacher_assignment_form.html`, `teacher_quiz_editor.html`) all
post to routes that don't exist (see `docs/known_bugs.md` #10) — so either they're leftover
scaffolding from an earlier design that should be deleted, or the real intended feature (a
point-and-click subject/assignment/quiz editor) was never actually built. Worth deciding which.

---

## Dead service scaffolding from an earlier architecture

| Priority | Status | Notes |
|---|---|---|
| | | |

Several service modules are unfinished stubs with no callers anywhere in `src/` — the real
pipeline is `services/check_core.py` + `services/docker_sandbox.py`, not these:
- `services/user_service.py` — all CRUD methods are `# TODO`.
- `services/testing/runner.py`, `services/testing/result_parser.py` — test
  execution/result-parsing, unimplemented.
- `services/ai/client.py`, `services/ai/code_reviewer.py` — OpenAI-based code review,
  unimplemented (the `AI_PROVIDER`/`OPENAI_*` settings in `.env` currently do nothing).
- `services/submission_checker.py` — docstring says "currently a stub... TBD";
  `check_submission()` unconditionally returns `(True, "")` regardless of input.

Either finish wiring these up (AI code review looks like a genuinely intended feature given
the `.env` has a real `OPENAI_API_KEY` and `AI_PROVIDER=openai` set, and `review_mode:
tests_then_ai` exists in the state machine / config schema) or delete the dead code so it
stops looking like a shipped capability.

---

## AI-assisted code review (`review_mode: tests_then_ai`) — config surface exists, service doesn't

| Priority | Status | Notes |
|---|---|---|
| | | |

`check_tasks.py` has a full branch for `tests_then_ai` and `tests_then_ai_then_teacher` review
modes that enqueues a `RUN_AI_REVIEW` outbox event, and `workers/tasks/review_tasks.py` /
`execute_ai_review_task` exists as an entry point — but the actual AI client
(`services/ai/client.py`) is an unimplemented stub. Any assignment configured with
`review_mode: tests_then_ai` will enqueue a job that (as far as this audit found) doesn't
produce a usable review. Worth a real smoke test with such a config to see current behavior.

---

## Teacher-scoped analytics don't exist — only ADMIN can see any analytics

| Priority | Status | Notes |
|---|---|---|
| | | |

`src/submissions_checker/api/routes/analytics.py` — both `GET /teacher/analytics` (dashboard)
and `GET /teacher/analytics/fraud` are hard-gated to `AdminUser`, with an explicit `# TODO
security: ... Gated to ADMIN until queries are scoped to subjects owned by the teacher` comment
in the code. So today, an ordinary teacher — even the owner of a subject — cannot see the
grade-distribution/pass-rate/quiz-difficulty dashboard or the anti-cheat fraud report for
their *own* students at all; only a superuser role can, and there's no self-serve way to create
one. (Per-student analytics, `GET /teacher/analytics/students/{id}`, IS correctly scoped to the
owning teacher and works today.)

---

## Openspec change tracker needs housekeeping (process gap, not a code gap)

| Priority | Status | Notes |
|---|---|---|
| | | |

Under `openspec/changes/` (not `archive/`), the following are 100%-complete per their
`tasks.md` and verified present in `src/`, but were never moved to `archive/`:
`gather-feedbacks`, `multilanguage-i18n-support`, `student-notifications`,
`test-mode-for-teacher`, `harden-anticheat-keyboard-shortcuts`.

The following are empty leftover directories (their real content, if any, only exists under a
dated `archive/` copy) and can likely just be deleted: `generate-checks-for-python-basics/`,
`retire-github-pr-ingest/`, `subject-config-apply/`, `fix-submission-extract-dir-permissions/`,
`notify-student-on-violation/`.

No genuinely *unstarted* open openspec change was found during this audit — everything left
open is either done-but-unarchived or an empty stub.

---

## `pythonBasicSubject` course content lives outside this repo

| Priority | Status | Notes |
|---|---|---|
| | | |

The real Python-basics course (9 labs, 10 variants each, custom `pythonbasics-checker:local`
Docker image, `checklib` grading library) lives in its own separate repo, not inside
submissions-checker. The identically-named folder at this repo's root — and
`plugins/pythonBasics/` — are empty placeholders. If the intent is for this subject to ship
as part of submissions-checker's default plugin set, it needs to be vendored/submoduled in
properly (and `HOST_PLUGINS_DIR` / the plugin directory naming convention reconciled — the
real repo's `subjectCode` is `pythonBasics` but its folder is named `pythonBasicSubject`,
which only resolves correctly today via careful directory-naming at deploy time). See
`docs/known_bugs.md` #12 for the current disconnect.

---

## No combined "teacher review + quiz" review mode

| Priority | Status | Notes |
|---|---|---|
| | | |

Surfaced 2026-07-06 while building out the `pythonBasics` subject's lab8 (OOP), which needs to
keep mandatory manual teacher review for 5 of its 10 variants (stateful/CRUD tasks with no
fixed I/O contract) while still wanting to send every student the same conceptual quiz.
`_advance_after_tests` (`workers/tasks/check_tasks.py`) treats `review_mode` as one
mutually-exclusive dispatch — `tests_then_teacher` never looks at a `quiz:` block, so a quiz
configured alongside it does not auto-fire after tests pass. It's still reachable (a teacher
approving a submission via `POST /teacher/submissions/{id}/review` correctly routes to
`QUIZ_SENT` when the assignment has a quiz, confirmed working end-to-end), just teacher-gated
instead of automatic. A `tests_then_teacher_then_quiz` mode (or a
`send_quiz_after_teacher_approval: true` flag) would remove the need to rely on the teacher
remembering the quiz exists. See `docs/pythonbasics_task_proposals.md` for the full context.

---

## Add your own items below

| Priority | Status | Notes |
|---|---|---|
| | | |
| | | |
| | | |
