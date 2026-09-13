## 1. Data model

- [x] 1.1 Add `current_index` (int, not null, default 0) and `question_started_at` (timestamptz, nullable) to `QuizAttempt`, and `timed_out` (bool, not null, default false) to `QuizAnswer` in `src/submissions_checker/db/models/quiz_template.py`
- [x] 1.2 Write Alembic revision `0023_quiz_per_question_timing` on top of `0022` adding those three columns, with a working `downgrade`
- [x] 1.3 Verify the migration applies and reverses against a scratch database (`alembic upgrade head` then `alembic downgrade -1`)

## 2. Check-free review modes

- [x] 2.1 Add `_QUIZ_FIRST_MODES = {"quiz_only", "quiz_then_teacher"}` and an `_accept_without_checks(submission, zip_path, review_mode)` helper to `workers/tasks/check_tasks.py` that safe-extracts the archive to a temp dir purely to validate it, discards it, sets `test_results = {"skipped": True, "reason": <mode>}`, and walks `start_validation` → `validation_passed` → `test_passed_quiz`
- [x] 2.2 Branch to that helper in `execute_check_task` after the plugin-config pinning block and before `check_core.resolve_check_plan`, so no check plan is resolved and no container starts
- [x] 2.3 Map `BadZipFile` / `OSError` / `UnsafeArchiveError` in the helper onto the existing `_fail_validation` with the same message shape as the sandbox path
- [x] 2.4 Add the `SubmissionStatus.QUIZ_SENT` row to `_TRANSITIONS` in `core/state_machine.py` with `quiz_passed` → `COMPLETED`, `quiz_passed_teacher` → `AWAITING_TEACHER_REVIEW`, `quiz_failed` → `FAILED`
- [x] 2.5 Replace the direct `submission.status = ...` assignments in `api/routes/student_quiz.py::_grade_and_finalize` with `transition()` calls using those events
- [x] 2.6 Snapshot `review_mode` into `config_snapshot` at attempt start in `api/routes/student_quiz.py`, and branch on it in `_grade_and_finalize`: `quiz_then_teacher` → `quiz_passed_teacher` + `enqueue_teacher_review_notification`, otherwise the existing complete-and-`finalize_grade` path
- [x] 2.7 Guard the quiz re-route in the teacher approve handler (`api/routes/teacher_portal.py`) so a submission that already has a passed quiz attempt completes instead of being sent back into the quiz
- [x] 2.8 Add `GET /teacher/submissions/{id}/download` serving the student's archive to the owning teacher (admins too), and link it from `templates/teacher_submission_review.html` — without it a `quiz_then_teacher` reviewer can only see the file's name

## 3. Per-question timing — server

- [x] 3.1 Resolve each drawn question's `time_limit_seconds` (question value → quiz `question_time_default_seconds` → none) in `_build_questions_from_config` and store it on the question snapshot
- [x] 3.2 Set `config_snapshot["per_question_timing"] = True` at attempt start when any drawn question resolved a limit
- [x] 3.3 Add `_question_seconds_remaining(attempt)` and `_advance_expired(attempt, db)` helpers: the latter loops while the current question's window has elapsed, writing a `timed_out` zero-point `QuizAnswer`, incrementing `current_index` and restamping `question_started_at`
- [x] 3.4 Rework `GET /portal/quiz/{attempt_id}` so that in stepper mode it runs `_advance_expired` first, finalizes the attempt when the cursor passes the last question, stamps `question_started_at` on first serve, and renders the single-question page
- [x] 3.5 Add `POST /portal/quiz/{attempt_id}/answer`: reject a stale `index` by redirecting to the current question; grade the answer with the existing `_grade_answer`, or record it `timed_out` with zero points when its window has already closed; advance the cursor, restamp, commit, and 303 back to the GET
- [x] 3.6 Make `POST /portal/quiz/{attempt_id}/submit` a redirect to the GET when the attempt is in stepper mode, leaving the single-page path unchanged
- [x] 3.7 Retarget the `reduce_time` anti-cheat action in stepper mode to move `question_started_at` back by `penalty_seconds` (expiring the question if that exhausts it), keeping the `_time_penalty_seconds` accumulation for single-page attempts

## 4. Per-question timing — UI

- [x] 4.1 Extract the inline anti-cheat and proctoring JS from `templates/student_quiz.html` into `templates/_quiz_anticheat.html` and include it back, with no behaviour change
- [x] 4.2 Create `templates/student_quiz_step.html`: progress indicator, per-question countdown reusing the amber→red styling, the single question rendered by type, one submit button, auto-submit at zero, and the anti-cheat include
- [x] 4.3 Add the timed-out badge per question and the "N questions ran out of time" summary line to `templates/student_quiz_result.html`
- [x] 4.4 Add the new `vocab.quiz.*` keys to `i18n/uk.yml`

## 5. Tests

- [x] 5.1 Unit: per-question seconds resolution and the `per_question_timing` flag in `_build_questions_from_config`
- [x] 5.2 Unit: `_advance_expired` burns exactly the elapsed windows and leaves the current question alone
- [x] 5.3 Unit: `compute_grade` with `works_pct=None` and `quiz_weight=1` returns the quiz percentage scaled into the grade band
- [x] 5.4 Functional: a `quiz_then_teacher` submission reaches `QUIZ_SENT` without a sandbox run, a passed quiz moves it to `AWAITING_TEACHER_REVIEW`, and teacher approval completes it with the quiz-derived grade
- [x] 5.5 Functional: a `quiz_only` submission completes on a passed quiz
- [x] 5.6 Functional: a corrupt or unsafe archive in a check-free mode still lands in `VALIDATION_FAILED`
- [x] 5.7 Functional: the stepper serves one question, rejects a stale `index`, auto-records a question whose window elapsed, and reports it on the result page
- [x] 5.8 Functional: a quiz with no per-question limits still renders and submits as a single page
- [x] 5.10 Functional: the owning teacher can download the attached archive; another teacher gets 403; a missing file gives 404
- [x] 5.9 Run the full suite plus `make lint` and `make type-check`, confirming no regression against the current baseline

## 6. Documentation

- [x] 6.1 Add a "Quiz block" reference section to `docs/PLUGIN_AUTHORING.md` covering every quiz key including the new timing ones, and add `quiz_only` / `quiz_then_teacher` to its review-mode table with a note that they need no `sandbox` block
- [x] 6.2 Document the stepper `reduce_time` semantics in `docs/anti-cheat.md`
