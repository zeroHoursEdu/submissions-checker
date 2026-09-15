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
- **ZIP only.** Submissions are ZIP uploads. The GitHub-PR / GitLab-MR ingest is **retired**
  — its enum members remain only to avoid a destructive DB migration; no code produces them.

---

## 1. Authentication & accounts

| Feature | Who | Route(s) |
|---|---|---|
| Log in (sets an 8-hour HTTP-only, strict-same-site JWT cookie; logs the login) | Any active account | `GET /auth/login`, `POST /auth/login` |
| Log out (clears the cookie) | Any account | `POST /auth/logout` |
| Forgot password (emails a single-use, ~2h reset link; response is uniform to avoid leaking which usernames exist) | Any account | `GET /auth/forgot-password`, `POST /auth/forgot-password` |
| Reset password (≥ 8 chars, typed twice; token single-use) | Any account | `GET /auth/reset-password`, `POST /auth/reset-password` |
| Choose interface language (English / Ukrainian; remembered ~1 year) | Anyone | `POST /set-language` |
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
| Soft-delete a subject (marks `DELETED`, data preserved) — **endpoint only, no UI button** | Owner only | `POST /teacher/subjects/{id}/delete` |
| Provision a TEST student for the subject (excluded from analytics) | Owner only | `POST /teacher/subjects/{id}/test-student` |
| Enter the portal **as** the test student (pilots the student journey) | Owner only | `POST /teacher/subjects/{id}/test-student/enter` |

> There is **no on-screen subject/assignment editor**. A subject's name, assignments,
> deadlines, grade ranges, review mode, late policy, attempt caps, variants, sandbox limits,
> and quiz/anti-cheat rules all come from the config ZIP — the single source of truth.
>
> The teacher UI carries **no affordance that mutates subject or assignment content**: no
> subject edit, no subject delete, no create/edit assignment, no quiz editor, no grade
> export button, and no analytics link. Those routes still exist and still enforce their
> own authorization, but the only path a teacher is offered is config re-apply.

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
| Review one submission (test results, AI review, submitted code) | Owner / ADMIN | `GET /teacher/submissions/{id}/review` |
| Approve / reject a submission (emails the student) — audited `teacher_approve_submission` / `teacher_reject_submission` | Owner / ADMIN | `POST /teacher/submissions/{id}/review` |
| Export grades CSV — **endpoint only, no UI button** | Owner / ADMIN | `GET /teacher/subjects/{id}/export.csv` |

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

A legacy flow (`CHECKING`, `WAITING_FOR_TEACHER_REVIEW`, `CHECK_FAILED`, …) is retained for
backward compatibility with existing data but is not produced by new submissions.

### Review modes (set per assignment in the config)

| `review_mode` | Path after tests pass | Manual grading? |
|---|---|---|
| `tests_only` | → COMPLETED | No |
| `tests_then_ai` | AI review → COMPLETED | No |
| `tests_then_teacher` | → AWAITING_TEACHER_REVIEW | Yes |
| `tests_then_ai_then_teacher` | AI review → AWAITING_TEACHER_REVIEW | Yes |
| `tests_then_quiz` | → QUIZ_SENT (student takes a quiz) | No |

Tests run in a locked-down sandbox (no internet, time- and memory-limited). Which per-test
names/details a student sees is controlled by the subject config.

## 5. Quizzes & proctoring / anti-cheat

| Feature | Who | Route(s) |
|---|---|---|
| Start / resume a quiz (consent required; resumes an in-progress attempt; redirects to result if already passed; enforces `max_quiz_attempts`) | STUDENT (owner) | `GET /portal/subjects/{subject}/assignments/{sa_id}/quiz` |
| Take the quiz (snapshotted questions, optional shuffle, optional timer) | STUDENT (owner) | `GET /portal/quiz/{attempt_id}` |
| Report an anti-cheat event (tab-switch, blur, copy, shortcut, fullscreen exit, …; may warn / flag / penalize time / fail) | STUDENT (owner) | `POST /portal/quiz/{attempt_id}/event` |
| Submit a webcam proctoring snapshot (when enabled + consented; skipped silently if storage absent) | STUDENT (owner) | `POST /portal/quiz/{attempt_id}/snapshot` |
| Submit the quiz (auto-graded; status COMPLETED / TIMED_OUT / VIOLATION_FAIL; pass marks the submission COMPLETED) | STUDENT (owner) | `POST /portal/quiz/{attempt_id}/submit` |
| See quiz result (score, pass/fail, per-question breakdown; correct answers only if the teacher enabled it) | STUDENT (owner) | `GET /portal/quiz/{attempt_id}/result` |

Question types (`QuizQuestionType`): `SINGLE_CHOICE`, `MULTIPLE_CHOICE`, `ORDERING`,
`TRUE_FALSE`, `SHORT_ANSWER` (short answers are recorded but not auto-scored). Quiz
violation flags and webcam thumbnails surface to the teacher on the assignment review board
(§4).

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

## 8. Analytics

| Feature | Who | Route(s) |
|---|---|---|
| Platform overview (headline scalars, grade histogram, per-subject + difficulty tables, exhausted-quiz failures) — aggregates across **all** teachers; **no dashboard link**, enter the URL | **ADMIN only** | `GET /teacher/analytics` |
| Fraud / anti-cheat dashboard (risk-scored integrity signals + login-activity overview) | **ADMIN only** | `GET /teacher/analytics/fraud` |
| Single-student profile (cross-subject grades, timeline, login stats) | TEACHER (own students) / ADMIN (any) | `GET /teacher/analytics/students/{id}` |

> The two aggregate dashboards are ADMIN-only because their queries are not yet scoped per
> teacher (tracked `TODO security`). The single-student profile is open to the owning
> teacher, with an object-level check (a non-admin teacher may only open a student enrolled
> in a subject they own); ADMIN bypasses that check.

## 9. Admin

| Feature | Who | Route(s) |
|---|---|---|
| Admin dashboard (user counts by role, outbox state, 20 most recent audit entries) | ADMIN only | `GET /admin` |
| List all users | ADMIN only | `GET /admin/users` |
| Activate / deactivate accounts | ADMIN only | `POST /admin/users/{id}/toggle-active` |
| Create teacher accounts | ADMIN only | `GET`/`POST /admin/teachers/create` |
| Audit log (append-only, up to 200 recent) | ADMIN only | `GET /admin/audit` |
| All teacher features on any subject (object-level authz bypass) | ADMIN | the `/teacher/*` routes above |

See the [Admin Journey guide](admin_journey_guide.md) for full detail, including which
actions are written to the audit log.
