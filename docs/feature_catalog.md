# Feature Catalogue — submissions-checker

A single, consolidated index of **every** feature in submissions-checker, across all three
roles, derived from the actual routes (`src/submissions_checker/api/routes/*.py`), the
submission state machine (`core/state_machine.py`), and the model enums
(`db/models/enums.py`). For the full narrative walkthroughs, follow the role guides:

- [Student Journey guide](student_journey_guide.md)
- [Teacher Journey guide](teacher_journey_guide.md)
- [Admin Journey guide](admin_journey_guide.md)

Each row below gives a one-line description, **who** can use it (role / permission), and the
**route(s)**. Where a feature has its own section in a guide, that guide is the source of
detail.

## Conventions used in this catalogue

- **Roles:** `STUDENT`, `TEACHER`, `ADMIN`. A capability marked `TEACHER/ADMIN` is open to
  both; `ADMIN only` is restricted to admins.
- **Ownership rule.** Subject-scoped teacher pages enforce `require_subject_access`: a
  teacher must own the subject (`Subject.owner_id`) or get a 403. **ADMIN bypasses this**
  on every subject-scoped page (object-level authz bypass).
- **No self-registration.** Admins create teachers; teachers create students; students
  never self-enroll. There is no public sign-up.
- **ZIP only.** Submissions are ZIP uploads; the GitHub-PR / GitLab-MR ingest was retired
  and its enum values dropped in migration 0027.

---

## 1. Authentication & accounts

| Feature | Who | Route(s) |
|---|---|---|
| Log in (sets an 8-hour HTTP-only, strict-same-site JWT cookie; logs the login) | Any active account | `GET /auth/login`, `POST /auth/login` |
| Log out (clears the cookie) | Any account | `POST /auth/logout` |
| Forgot password (emails a single-use, ~2h reset link; response is uniform to avoid leaking which usernames exist) | Any account | `GET /auth/forgot-password`, `POST /auth/forgot-password` |
| Reset password (≥ 8 chars, typed twice; token single-use) | Any account | `GET /auth/reset-password`, `POST /auth/reset-password` |
| Create teacher account (username + password ≥ 8 chars; bcrypt-hashed) — audited `create_teacher` | ADMIN only | `GET /admin/teachers/create`, `POST /admin/teachers/create` |
| List all accounts | ADMIN only | `GET /admin/users` |
| Activate / deactivate an account (cannot deactivate self; inactive accounts can't authenticate) — audited `toggle_user_active` | ADMIN only | `POST /admin/users/{id}/toggle-active` |
| Create student account in bulk / singly | TEACHER/ADMIN | see §3 Enrollment |
| Health / readiness probes | Public (infra) | `GET /health`, `GET /health/ready` |
| Internal user API (skeleton CRUD) | API | `POST /api/v1/users`, `GET /api/v1/users/{id}` |

> Deactivated accounts are rejected by the session guard (`_get_current_user`) with
> 401 "User inactive" on their next request.

## 2. Subjects & config

| Feature | Who | Route(s) |
|---|---|---|
| Teacher dashboard (your active subjects + enrolled counts) | TEACHER/ADMIN | `GET /teacher` |
| Create / update a subject from a **config ZIP** (versioned, content-hashed, deduplicated; upsert by `subjectCode`; uploader becomes owner of a new subject) | TEACHER/ADMIN (update requires ownership) | `POST /teacher/subjects/apply-config` |
| View a subject (students, assignments, test-student + feedback panels) | Owner / ADMIN | `GET /teacher/subjects/{id}` |
| Soft-delete a subject (marks `DELETED`, data preserved; Операції tab, checkbox-confirmed) | Owner / ADMIN | `POST /teacher/subjects/{id}/delete` |
| Provision a TEST student for the subject (excluded from analytics) | Owner only | `POST /teacher/subjects/{id}/test-student` |
| Enter the portal **as** the test student (pilots the student journey) | Owner only | `POST /teacher/subjects/{id}/test-student/enter` |

> There is **no on-screen subject/assignment editor**. A subject's name, assignments,
> deadlines, grade ranges, review mode, late policy, attempt caps, variants, sandbox limits,
> and quiz/anti-cheat rules all come from the config ZIP — the single source of truth.
>
> The teacher UI carries **no affordance that mutates subject or assignment content**: no
> subject edit, no create/edit assignment, no quiz editor. Subject delete and grade export
> live on the Операції tab; everything about content goes through config re-apply.

## 3. Enrollment & students

| Feature | Who | Route(s) |
|---|---|---|
| Download enrollment example CSV (`email,variant`; one row per variant declared in the subject's config, placeholder `example.invalid` addresses) | Owner / ADMIN | `GET /teacher/subjects/{id}/students/template.csv` |
| Enrol **existing** students into a subject from `email,variant` (fans out per-assignment records, sets the variant on all of them; unknown e-mails rejected per row; never creates accounts or sends e-mail; ≤ 1 MB UTF-8) | Owner / ADMIN | `POST /teacher/subjects/{id}/students/import` |
| Global student import (creates accounts only, no enrollment) | TEACHER/ADMIN | `POST /teacher/students/import`, sample `GET /teacher/students/sample.csv` |
| Add a single student — audited `add_student` | TEACHER/ADMIN | `GET /teacher/students/add`, `POST /teacher/students/add` |
| Browse the full roster (account/email/login status) | TEACHER/ADMIN | `GET /teacher/students` |
| Enroll a student into a subject — audited `enroll_student` | Owner / ADMIN | `POST /teacher/subjects/{id}/enroll/{student_id}` |
| Unenroll a student — audited `unenroll_student` | Owner / ADMIN | `POST /teacher/subjects/{id}/unenroll/{student_id}` |
| One-time proctoring consent (required before any quiz) | STUDENT | `GET /portal/consent`, `POST /portal/consent` |
| See enrolled subjects (grid with progress) | STUDENT | `GET /portal` |
| Cross-subject standing (average grade, upcoming/overdue work) | STUDENT | `GET /portal/summary` |
| See a subject's assignments | STUDENT (enrolled) | `GET /portal/subjects/{subject}` |
| Open one assignment (brief, files, history, actions) | STUDENT (owner of the record) | `GET /portal/subjects/{subject}/assignments/{sa_id}` |

## 4. Submissions & the checking pipeline

| Feature | Who | Route(s) |
|---|---|---|
| Submit work as a ZIP (≤ 50 MB; deadline/late-policy, completed-once, and `max_submissions` checks; queued for checking; similarity score recorded) — audited `student_submit` | STUDENT (owner) | `POST /portal/subjects/{subject}/assignments/{sa_id}/submit` |
| Watch checking progress (refresh the assignment page) | STUDENT (owner) | `GET /portal/subjects/{subject}/assignments/{sa_id}` |
| Assignment review board (per-student latest submission, grade, integrity flags) | Owner / ADMIN | `GET /teacher/subjects/{id}/assignments/{sa_id}` |
| Review one submission (test results, AI verdict when the mode ran one, proctoring evidence, submitted archive) | Owner / ADMIN | `GET /teacher/submissions/{id}/review` |
| Approve / reject a submission (emails the student) — audited `teacher_approve_submission` / `teacher_reject_submission` | Owner / ADMIN | `POST /teacher/submissions/{id}/review` |
| Export grades CSV (Операції tab) | Owner / ADMIN | `GET /teacher/subjects/{id}/export.csv` |

### The submission state machine

Submissions move through statuses via the events defined in `core/state_machine.py`.
The current ("precise") flow:

```
PENDING ──start_validation──▶ VALIDATING
VALIDATING ──validation_passed──▶ TESTING
           ──validation_failed──▶ VALIDATION_FAILED
TESTING ──test_failed──▶ TEST_FAILED
        ──test_passed_tests_only──▶ COMPLETED
        ──test_passed_ai──▶ AWAITING_AI_REVIEW
        ──test_passed_teacher──▶ AWAITING_TEACHER_REVIEW
        ──test_passed_quiz──▶ QUIZ_SENT
AWAITING_AI_REVIEW ──start_ai_review──▶ AI_REVIEWING
AI_REVIEWING ──ai_review_done_teacher──▶ AWAITING_TEACHER_REVIEW
             ──ai_review_done_completed──▶ COMPLETED
             ──ai_review_failed──▶ AI_REVIEW_FAILED
AI_REVIEW_FAILED ──retry_ai_review──▶ AI_REVIEWING
AWAITING_TEACHER_REVIEW ──teacher_approve──▶ COMPLETED
                        ──teacher_reject──▶ FAILED
                        ──teacher_send_quiz──▶ QUIZ_SENT
```

### Review modes (set per assignment in the config)

| `review_mode` | Path after tests pass | Manual grading? |
|---|---|---|
| `tests_only` | → COMPLETED | No |
| `tests_then_ai` | AI review → COMPLETED | No |
| `tests_then_teacher` | → AWAITING_TEACHER_REVIEW | Yes |
| `tests_then_ai_then_teacher` | AI review → AWAITING_TEACHER_REVIEW | Yes |
| `tests_then_quiz` | → QUIZ_SENT (student takes a quiz) | No |

AI verdicts (cheating / AI-generated flags with confidence and reason, a code mark, a
student-facing comment) are shown to the teacher on the review page and as a red badge on the
assignment board; the comment reaches the student only when `ai_review.show_comment_to_student`
is set. See `docs/PLUGIN_AUTHORING.md` › AI Review Block.

Tests run in a locked-down sandbox (no internet, time- and memory-limited). Which per-test
names/details a student sees is controlled by the subject config.

## 5. Quizzes & proctoring / anti-cheat

| Feature | Who | Route(s) |
|---|---|---|
| Start / resume a quiz (consent required; resumes an in-progress attempt; redirects to result if already passed; enforces `max_quiz_attempts`) | STUDENT (owner) | `GET /portal/subjects/{subject}/assignments/{sa_id}/quiz` |
| Take the quiz (snapshotted questions, optional shuffle, optional timer) | STUDENT (owner) | `GET /portal/quiz/{attempt_id}` |
| Report an anti-cheat event (tab-switch, blur, copy, shortcut, fullscreen exit, …; may warn / flag / penalize time / fail) | STUDENT (owner) | `POST /portal/quiz/{attempt_id}/event` |
| Submit a webcam proctoring snapshot (browser-side MediaPipe face detection, models served from `/static/vendor/`; when enabled + consented; skipped silently if storage absent) | STUDENT (owner) | `POST /portal/quiz/{attempt_id}/snapshot` |
| Submit the quiz (auto-graded; status COMPLETED / TIMED_OUT / VIOLATION_FAIL; pass marks the submission COMPLETED) | STUDENT (owner) | `POST /portal/quiz/{attempt_id}/submit` |
| See quiz result (score, pass/fail, per-question breakdown; correct answers only if the teacher enabled it) | STUDENT (owner) | `GET /portal/quiz/{attempt_id}/result` |
| Report a question as incorrect / invalid (non-blocking: works mid-attempt and from the result page, changes no answer and no clock; unlimited) | STUDENT (owner) | `POST /portal/quiz/{attempt_id}/dispute` |
| Review reported questions | TEACHER (subject owner) / ADMIN | `GET /teacher/disputes`, `GET /teacher/disputes/{id}` |
| Rule on a reported question — a note is mandatory either way | TEACHER (subject owner) / ADMIN | `POST /teacher/disputes/{id}/resolve` |
| Pause the attempt for an air raid (verified against alerts.in.ua for the student's location) | STUDENT (owner) | `POST /portal/quiz/{attempt_id}/airraid/pause` |
| Resume after an air raid | STUDENT (owner) | `POST /portal/quiz/{attempt_id}/airraid/resume` |

Question types (`QuizQuestionType`): `SINGLE_CHOICE`, `MULTIPLE_CHOICE`, `ORDERING`,
`TRUE_FALSE`. Any other type is rejected when the config is applied. Quiz
violation flags and webcam thumbnails surface to the teacher on the assignment review board
(§4).

### Reported questions ("disputes")

A student who thinks a question is wrong or unanswerable flags it with a button next to the
question. The report reaches the subject owner's notification bell (all active admins when
the subject has no owner) with a link straight to the ruling panel; there is no email, since
the review digest is keyed per submission and would coalesce disputes away.

In the panel the teacher sees the question **as that student saw it** — option order is
shuffled per attempt, so the snapshot's key is the only one their stored answer index means
anything against — plus the student's answer, their note, and how many attempts an accept
would re-score. A note explaining the decision is required for accept and reject alike,
because the student is shown that text verbatim.

Accepting writes a `quiz_question_overrides` row for `(plugin_config_id,
plugin_config_version, question_id)` and credits the question to **everyone who drew it from
that config version**:

- finished attempts that are not already passing are re-scored immediately (`max_score` is
  untouched, so only the earned score rises), and an attempt that never reached the question
  gets a credited answer row;
- attempts still in progress are left alone and read the override when they finalize —
  writing an answer row for a live attempt would be double-counted, because answering always
  INSERTs and `quiz_answers` has no uniqueness on `(attempt_id, question_id)`;
- `VIOLATION_FAIL` attempts are excluded: they failed for cheating, not for a bad question;
- re-uploading the subject config bumps the version, so a repaired question stops being
  credited rather than crediting whatever moved into its index.

Where that flips an attempt to passing, the submission follows — including out of a terminal
`FAILED`, via the `dispute_regrade_passed` / `dispute_regrade_passed_teacher` transitions
added for exactly this (named distinctly so an ordinary late-finishing attempt can never
resurrect a failed submission). `finalize_grade` then rewrites the grade. Every affected
student is notified, including classmates who never reported anything. Accepting also closes
every other open report on the same question, since reports are unlimited.

### Air-raid pause

A student under an air-raid alert presses a button next to the report control; the browser
asks for their location and posts the coordinates. The backend maps them to an
alerts.in.ua oblast (bundled boundaries — the API has no coordinate endpoint) and pauses the
attempt only if an `air_raid` alert is actually active there. Every other outcome refuses
with a reason the page explains: geolocation denied (never even reaches the server), outside
Ukraine, no active alert, or the provider unreachable/unconfigured. An unverified claim never
stops a graded clock.

While paused:

- **every clock is frozen** — one `_effective_now` helper stops "now" for the attempt, and
  `paused_seconds` accumulates closed pauses, so nothing can time out and no per-question
  window can burn;
- **the questions are not sent at all** — `student_quiz_paused.html` renders no question
  text, options or answer form, so pausing is not a free read;
- **anti-cheat is suspended** — that template omits the anti-cheat partial, so the camera
  gate and every violation listener are gone; violation events and snapshot uploads are
  ignored server-side, and answering or submitting is rejected.

Only the student ends the pause, whenever they choose; nothing re-checks the alert. The pause
survives closing the tab (re-entering shows the same question and the same timer value), and
an attempt that is never resumed stays `IN_PROGRESS` indefinitely — there is no reaper.
Each pause is recorded in `quiz_attempt_pauses` with the coordinates, region and the alert's
start time, so a suspicious pause can be audited afterwards.

Configuration: `ALERTS_IN_UA_TOKEN` (absent ⇒ feature reports unavailable),
`AIR_RAID_PAUSE_ENABLED`, `AIR_RAID_CACHE_SECONDS`. Geolocation needs a secure context, so
the pause button does nothing over plain HTTP other than `localhost`.

Two known limits, both recorded in `docs/known_bugs.md`: region resolution is oblast-level,
so a student within a kilometre or two of an oblast border can resolve to the neighbour (or
to nothing, which refuses the pause); and in single-page mode the tab that pressed pause has
already loaded every question, so stopping the clock gives unbounded time to research
questions they have seen. Per-question (stepper) mode bounds that to one question.

## 6. Feedback

| Feature | Who | Route(s) |
|---|---|---|
| Request course feedback (one request per subject per current semester; tokenized link emailed to each enrolled student; errors `no_active_semester` / `already_sent`) | Owner / ADMIN | `POST /teacher/subjects/{id}/feedback/request` |
| View collected feedback + average rating (current semester) | Owner / ADMIN | `GET /teacher/subjects/{id}/feedback` |
| Export feedback CSV | Owner / ADMIN | `GET /teacher/subjects/{id}/feedback/export.csv` |
| Submit feedback (1–5 rating + three free-text fields; public, no login, single-use token) | Anyone with the link | `GET /feedback/{token}`, `POST /feedback/{token}`, thanks `GET /feedback/{token}/thanks` |

## 7. Notifications & digests

| Feature | Who | Route(s) |
|---|---|---|
| In-app notification inbox (latest 50, newest first; you only see your own) | Any account | `GET /notifications` |
| Mark one notification read | Any account | `POST /notifications/{id}/read` |
| Mark all read | Any account | `POST /notifications/read-all` |
| Unread count (for the badge) | Any account | `GET /notifications/unread-count` |
| Email preferences (per-case EMAIL on/off: `SUBMISSION_CHECKED`, `FEEDBACK_REQUEST`) | STUDENT | `GET /portal/notification-preferences`, `POST /portal/notification-preferences/{case}/{method}/toggle` |
| Coalesced teacher review digest (background job batches a teacher's pending review notices into one email) | TEACHER/ADMIN (automatic) | — (no route; `teacher_notification_queue` + background job) |

## 8. Admin

| Feature | Who | Route(s) |
|---|---|---|
| Admin dashboard (user counts by role, outbox state, 20 most recent audit entries) | ADMIN only | `GET /admin` |
| List all users | ADMIN only | `GET /admin/users` |
| Activate / deactivate accounts | ADMIN only | `POST /admin/users/{id}/toggle-active` |
| Create teacher accounts | ADMIN only | `GET`/`POST /admin/teachers/create` |
| Audit log (append-only, up to 200 recent) | ADMIN only | `GET /admin/audit` |
| Semesters: list, add, edit (no overlaps; audited) | ADMIN only | `GET /admin/semesters`, `POST /admin/semesters`, `POST /admin/semesters/{id}` |
| All teacher features on any subject (object-level authz bypass) | ADMIN | the `/teacher/*` routes above |

See the [Admin Journey guide](admin_journey_guide.md) for full detail, including which
actions are written to the audit log.
