# Teacher Journey — Full Guide

This is the end-to-end guide for **teachers** using submissions-checker. It is written
for educators, not engineers: each feature explains *what it does*, *where to find it*,
*what you need to provide*, *who is allowed to use it*, and *what happens as a result*.

For the more compact route-by-route reference, see [teacher-journey.md](teacher-journey.md).
Related deep-dives: [PLUGIN_AUTHORING.md](PLUGIN_AUTHORING.md) (how subjects are built),
[anti-cheat.md](anti-cheat.md) (quiz proctoring), and
[student-journey.md](student-journey.md) (the student side of the same flow).

---

## Before you start: roles and what they can do

The system has three account roles. Your capabilities depend on your role.

| Role | What it can do |
|---|---|
| **TEACHER** | Everything in this guide that is scoped to *subjects you own*: apply subject configs, enroll students, review and grade submissions, send feedback requests, export grades, view a student's profile. |
| **ADMIN** | Everything a teacher can do on *any* subject, **plus** account management (create teachers, deactivate accounts, audit log) **plus** the cross-platform analytics and fraud dashboards. |
| **STUDENT** | The student portal only (submit work, take quizzes) — see [student-journey.md](student-journey.md). |

> **Ownership rule.** A teacher only sees and acts on subjects they own
> (`Subject.owner_id`). The helper `require_subject_access` enforces this on every
> subject-scoped page: if you are not the owner (and not an ADMIN), you get a 403
> "Not authorized for this subject". ADMIN bypasses this and can access all subjects.

> **You cannot create your own teacher account.** There is no public sign-up for
> teachers. An ADMIN creates your account (see [Account management](#account-management-admins-only)
> at the end). The same applies to students — *you* create their accounts, they never
> self-register.

---

## Feature catalogue (quick reference)

| # | Feature | Page / route | Inputs | Permission | Outcome |
|---|---------|--------------|--------|------------|---------|
| 1 | Log in | `GET/POST /auth/login` | username + password | any account | JWT cookie set (8h), redirected to `/teacher` |
| 1 | Forgot / reset password | `/auth/forgot-password`, `/auth/reset-password` | username, then new password | any account | reset link emailed (single-use, 2h) |
| 2 | Dashboard | `GET /teacher` | — | TEACHER/ADMIN | list of your active subjects + enrolled counts |
| 3 | Create / update a subject | `POST /teacher/subjects/apply-config` | a config ZIP | TEACHER/ADMIN | subject + assignments upserted; you become owner |
| 3 | Delete a subject | `POST /teacher/subjects/{id}/delete` | — | subject owner only | subject marked DELETED |
| 3 | Subject detail | `GET /teacher/subjects/{id}` | — | owner / ADMIN | students, assignments, test-student panel, feedback panel |
| 4 | Download enrollment example | `GET /teacher/subjects/{id}/students/template.csv` | — | owner / ADMIN | `email,variant` CSV, one row per variant in the config |
| 4 | Enrol students (per subject) | `POST /teacher/subjects/{id}/students/import` | `email,variant` CSV (≤1 MB) | owner / ADMIN | existing students enrolled, variants set; no accounts created, no e-mail |
| 4 | Import students (global) | `POST /teacher/students/import` | CSV (≤1 MB) | TEACHER/ADMIN | accounts created (no enrollment) |
| 4 | Add one student | `GET/POST /teacher/students/add` | name, email, group, optional GitHub | TEACHER/ADMIN | one account created, credentials emailed |
| 4 | Browse all students | `GET /teacher/students` | — | TEACHER/ADMIN | roster with account/email/login status |
| 4 | Enroll / unenroll | `POST /teacher/subjects/{id}/enroll/{student_id}` / `…/unenroll/{student_id}` | — | owner / ADMIN | student added/removed from subject |
| 5 | Provision test student | `POST /teacher/subjects/{id}/test-student` | — | owner only | a TEST student account is created for the subject |
| 5 | Enter as test student | `POST /teacher/subjects/{id}/test-student/enter` | — | owner only | you are logged in as the test student |
| 6 | Assignment review board | `GET /teacher/subjects/{id}/assignments/{sa_id}` | — | owner / ADMIN | per-student submission, grade, and integrity flags |
| 6 | Review a submission | `GET /teacher/submissions/{id}/review` | — | owner / ADMIN | test results, AI review, submitted code |
| 6 | Act on a submission | `POST /teacher/submissions/{id}/review` | approve / reject (+ reason) | owner / ADMIN | submission advanced; student emailed |
| 7 | Export grades | `GET /teacher/subjects/{id}/export.csv` | — | owner / ADMIN | CSV of grades; **no UI button** — endpoint only |
| 8 | Analytics overview | `GET /teacher/analytics` | — | **ADMIN only** | platform-wide stats and charts; **no dashboard link** — enter the URL |
| 8 | Fraud detection | `GET /teacher/analytics/fraud` | — | **ADMIN only** | risk-scored integrity flags |
| 8 | Student profile | `GET /teacher/analytics/students/{id}` | — | TEACHER (own students) / ADMIN | one student's grades + login history |
| 9 | Request course feedback | `POST /teacher/subjects/{id}/feedback/request` | — | owner / ADMIN | tokenized links emailed to enrolled students |
| 9 | View feedback | `GET /teacher/subjects/{id}/feedback` | — | owner / ADMIN | responses + average rating |
| 9 | Export feedback | `GET /teacher/subjects/{id}/feedback/export.csv` | — | owner / ADMIN | CSV of responses |
| 10 | In-app notifications | `GET /notifications` (+ read/read-all) | — | any account | your unread review/submission alerts |

> Cells marked **ADMIN only** are a current limitation, not a design choice — see
> [the analytics note](#a-note-on-who-can-see-analytics).

---

## 1. Log in

**What it does.** Authenticates you and gives you a session.

**How.** Go to `GET /auth/login` and submit your username and password
(`POST /auth/login`). On success a JSON Web Token is stored in an HTTP-only,
strict-same-site cookie that lasts **8 hours**. Teachers are redirected to `/teacher`;
students to `/portal`. Every successful login is recorded (used later by fraud analytics).

**Forgot your password?**
- `GET /auth/forgot-password` → enter your username → `POST`. If the account exists and
  has an email on file, a reset link is emailed. (The page always says "sent" regardless,
  to avoid revealing which usernames exist.)
- The link opens `GET /auth/reset-password?token=…`. Set a new password (≥ 8 characters,
  typed twice). The token is **single-use** and expires after **2 hours**.

**Log out.** `POST /auth/logout` clears the cookie.

**Permission.** Any active account. Deactivated accounts cannot log in.

---

## 2. The teacher dashboard

**What it does.** Your home screen — `GET /teacher`. Lists every **active** subject you
own, each with its enrolled (real) student count and a link into the subject.

**Outcome.** From here you reach every other teacher feature. After uploading a config,
a green success / red error banner appears here reporting what happened.

---

## 3. Create and manage subjects (via config ZIP)

> **Important:** there is **no on-screen form** for creating a subject or typing in
> assignments, deadlines, or grades. Everything about a subject — its name, description,
> assignments, deadlines, grade ranges, review flow, quiz, and anti-cheat rules — is
> defined in a **config package** and uploaded as a ZIP. This is the single source of truth.

### 3.1 What a config ZIP contains

A config ZIP is a folder, zipped, containing:

- `config.yml` — the subject definition (name, description, and one block per assignment).
- Per-assignment **check scripts** (`check.py`, optional `validate.py`) that grade student
  uploads automatically inside a locked-down sandbox.
- Optional **content files** students can download (PDF briefs, datasets, etc.).
- Optional **images** (`grid.png`, `main.png`) used as the subject's thumbnail and banner.

The full schema — assignments, `deadline`, `min_grade`/`max_grade`, `review_mode`,
`late_policy`, `max_submissions`, `variants`, sandbox limits, and the quiz/anti-cheat
blocks — is documented in [PLUGIN_AUTHORING.md](PLUGIN_AUTHORING.md). You do **not** need
to know Python to *upload* one, but the check scripts inside it are written by whoever
authors the subject.

### 3.2 Upload / update a subject

**How.** On the dashboard, upload your ZIP — this calls
`POST /teacher/subjects/apply-config`.

**What happens.**
- The ZIP is parsed and content-hashed. The result is stored as a **versioned**
  `SubjectPluginConfig`.
- **Deduplication:** re-uploading byte-identical content is a no-op (no new version).
- **Upsert by code:** the subject is matched by its `subjectCode`. If it doesn't exist it
  is created and **you become its owner**; if it exists, it (and its assignments) are
  updated. Only one ACTIVE subject may exist per code.
- **In-flight safety:** submissions already being checked keep using the config version
  they started with; new submissions use the new version.

**Outcome.** You are redirected to the dashboard with a banner reporting whether the
subject was *created* or *updated*. Errors (bad ZIP, invalid YAML, a permission problem)
are shown in a red banner.

**Permission.** Any TEACHER/ADMIN may upload. Updating an existing subject requires that
you own it (enforced inside the apply service).

### 3.3 View a subject

`GET /teacher/subjects/{id}` shows everything for one subject:
- the enrolled **real** students (grouped by their study group),
- the assignments (ordered by deadline),
- the **test-student** panel (see §5) — only if you own the subject,
- the **course-feedback** panel for the current semester (see §9).

### 3.4 Delete a subject

`POST /teacher/subjects/{id}/delete` marks the subject **DELETED** (a soft delete — data
is preserved). **Only the owner** can do this; even another teacher gets a 403.

There is **no button for this in the UI**. Deleting a subject is as consequential as
creating one, so it belongs with the same deliberate, reviewed process — see
[§3.5](#35-subject-content-changes-only-through-config-re-apply).

### 3.5 Subject content changes only through config re-apply

The subject page and the assignment page deliberately offer **no** way to edit a subject,
create or edit an assignment, edit a quiz, or export grades. The config ZIP is the single
source of truth: it lives in the subject repository, it is reviewed in a diff, and every
apply stores a new numbered `SubjectPluginConfig` version. A change made through a form
would exist only in the database and would be silently overwritten by the next apply.

To change anything about a subject or its assignments — a deadline, grading weights, a
quiz bank — edit `config.yml`, re-zip, and upload it again through Apply config. Re-apply
is idempotent and deduplicated by content hash, and quiz attempts already in flight finish
on the version they started with.

The routes behind the removed buttons still exist and still enforce their own
authorization, so API clients and admin tooling are unaffected.

---

## 4. Enrolling and managing students

You create student accounts; students never self-register. Account creation (§4.2, §4.3)
and enrolment into a subject (§4.1, §4.4) are separate steps.

### 4.1 Enrolling students into a subject (recommended)

This is the path that puts students into a subject and sets their **variant**. It enrols
only: the students must already exist, which means you run the global import (§4.2) first.

1. **Download the example:** `GET /teacher/subjects/{id}/students/template.csv`, linked
   from the enrolment panel on the subject page. It has two columns, `email,variant`, and
   one row per variant the subject's own config declares — so the identifiers you copy are
   the ones the checker will accept. A subject with no variants gets two example rows with
   the variant cell empty. The placeholder addresses are on `example.invalid`, which can
   never belong to a real person, so uploading the file unedited enrols nobody.
2. **Fill it in** with one row per student.
3. **Upload it** from the same panel (max 1 MB, UTF-8).

**What happens per row:**
- The `email` is trimmed, lower-cased and matched against existing students. An address
  that matches nobody is **rejected** — no student, account or invitation is created for
  it — and the remaining rows are still processed.
- The student is enrolled in the subject if not already, and a per-assignment record is
  created for **every** assignment of the subject.
- A non-empty `variant` is written to all of those records. An **empty** variant cell
  leaves any stored value untouched, so re-enrolling never flattens variants set elsewhere.
- Re-uploading the same file is safe: nothing is duplicated.

**Outcome.** Redirect back to the subject page with counts — newly enrolled, already
enrolled, rejected — and each rejected row listed by its line number (the header is line 1)
with the reason. The list is capped at 20 rows plus an overflow count.

**No e-mail is sent by this step.** Credentials come from the global import (§4.2), so
enrolment can be re-run as often as you like without re-inviting anyone.

### 4.2 Global bulk import

`POST /teacher/students/import` (sample at `GET /teacher/students/sample.csv`) creates
accounts from `student_group, student_name, student_surname, email` but **does not enroll**
them in any subject, and it is the step that **sends the credentials e-mail**. Run it
first, then enrol with §4.1. Same 1 MB / UTF-8 limits.

### 4.3 Add a single student

`GET /teacher/students/add` → `POST /teacher/students/add`. Provide first name, last name,
email, group, and optionally a GitHub username. Creates the account and queues a
credentials email. Duplicate emails are rejected with a message.

### 4.4 Enroll / unenroll existing students

- `POST /teacher/subjects/{id}/enroll/{student_id}` — enrolls the student and creates their
  per-assignment records for the subject.
- `POST /teacher/subjects/{id}/unenroll/{student_id}` — removes the enrollment.

Both require subject access (owner/ADMIN) and are recorded in the audit log.

### 4.5 Browse the roster

`GET /teacher/students` lists **all** students with, per student: group, username, whether
the account is active, the state of their credentials email (pending/finished/error), and
their first-login timestamp. This is the place to confirm that invitations actually went out.

---

## 5. Pilot a subject as a test student

**What it does.** Lets you walk the entire student experience — submit a ZIP, watch checks
run, take the quiz — **before** real students are exposed to it. Test students are tagged
`TEST` and are **excluded from analytics** so they never pollute your statistics.

**How.**
1. `POST /teacher/subjects/{id}/test-student` provisions a dedicated test account for the
   subject (enrolled in every assignment). The subject page then shows its username and
   password. Calling it again when one already exists is a no-op.
2. `POST /teacher/subjects/{id}/test-student/enter` logs *you* into the portal **as** that
   test student (it swaps your session cookie). Walk through the student journey, then log
   out and log back in as yourself.

**Permission.** Owner only — both endpoints reject non-owners with a 403.

---

## 6. Reviewing and grading submissions

How much of this you do depends on the assignment's `review_mode` (set in the config):

| `review_mode` | What you do |
|---|---|
| `tests_only` | Nothing — auto-graded and COMPLETED. |
| `tests_then_ai` | Nothing — AI review then COMPLETED. |
| `tests_then_teacher` | **You grade** every submission that passed tests. |
| `tests_then_ai_then_teacher` | You grade after the AI review. |
| `tests_then_quiz` | A quiz is sent to the student; no manual grading. |

### 6.1 The assignment review board

`GET /teacher/subjects/{id}/assignments/{sa_id}` is the per-assignment table. For each
enrolled student it shows their latest submission, its status, when it was submitted, and
the current grade. It also surfaces **integrity flags** from any associated quiz:

- a **violations** badge (e.g. tab-switching, copy attempts, auto-fail) — see
  [anti-cheat.md](anti-cheat.md);
- **webcam proctoring thumbnails** captured when a quiz violation was flagged (if the
  subject enabled webcam proctoring and the student consented).

### 6.2 Reviewing one submission

`GET /teacher/submissions/{id}/review` opens a submission that is awaiting your review. You
see the automated **test results**, the **AI review** (if the review mode included one),
and the **submitted code**. This page is only reachable for submissions in a
"waiting for teacher" state; others 404. Non-owners get a 403.

### 6.3 Approving or rejecting

`POST /teacher/submissions/{id}/review` with an action:

- **approve** — advances the submission. If the assignment has a quiz attached, approval
  *sends the quiz* (status → QUIZ_SENT) rather than finishing; otherwise it goes to
  COMPLETED.
- **reject** — fails the submission. You may attach a `reason`, which is stored and shown
  to the student.

In every case an **email is queued to the student** announcing the outcome, the action is
written to the **audit log**, and you are returned to the assignment board. (Note: there is
no free-text "set this exact grade" field on this endpoint — outcomes are approve/reject;
grade values come from the automated score and config grade range.)

---

## 7. Exporting grades

The **Операції** tab has an «Експорт оцінок (CSV)» button, and next to it a checkbox-confirmed
«Видалити предмет» that soft-deletes the subject (hidden everywhere, data kept).
`GET /teacher/subjects/{id}/export.csv` downloads a CSV of the subject's grades with
columns: student, email, group, assignment, grade, max grade, submission status, and
submission time. One row per student-per-assignment, sorted by group then name. Owner/ADMIN
only.

---

## 8. Analytics and academic-integrity reports

Three reports exist. **Two of them are currently restricted to ADMIN accounts.**

### 8.1 Overview dashboard — `GET /teacher/analytics` (ADMIN only)

Platform-wide headline numbers (total students, subjects, average grade, pass rate), a
**grade-distribution histogram** (10-point buckets), a **per-subject performance** table,
an **assignment-difficulty** ranking (hardest first), and a list of students who **failed a
quiz after exhausting all attempts**.

### 8.2 Fraud detection — `GET /teacher/analytics/fraud` (ADMIN only)

Risk-scored integrity signals (these are *signals, not proof*):
- **Late first login + high grade** — first-ever login within 24h before a deadline, grade
  ≥ 80% of max (+3 risk points).
- **Few logins + high average grade** — fewer than 3 logins, average grade ≥ 75
  (+2 points).
- **Single-day submission burst** — 3+ submissions all on one calendar day (+2 points).

Plus a login-activity overview of all students (never-logged-in students float to the top).
Risk levels: 0 none, 1–2 Low, 3–4 Medium, 5+ High.

### 8.3 Student profile — `GET /teacher/analytics/students/{id}` (TEACHER allowed)

A single student's cross-subject profile: per-subject grade summaries, a **grade timeline**
chart, an all-assignments table, and login statistics. **This page is open to teachers**,
with object-level authorization: a non-admin teacher may only open a student who is enrolled
in at least one subject they own — otherwise a 403.

### A note on who can see analytics

The overview and fraud pages query data across **every** teacher's subjects, so they are
deliberately locked to ADMIN until the queries are scoped per-owner (there is a tracked
`TODO security` in the code). As a teacher you can always see any individual student's
profile (§8.3); for the aggregate dashboards, ask an admin. (The older
[teacher-journey.md](teacher-journey.md) describes these as "gated accordingly" — this
guide states the gate explicitly: dashboard + fraud = ADMIN only today.)

---

## 9. Collecting course feedback

**What it does.** Sends each enrolled student a private, single-use link to an anonymous
course-feedback form (a 1–5 rating plus three free-text fields: what went well, what went
badly, what to change). Feedback is tied to the **current semester**.

**How.**
1. `POST /teacher/subjects/{id}/feedback/request`. The system finds the current semester
   (a semester whose date range contains today), creates one feedback request, generates a
   unique token per enrolled student, and queues an email to each.
   - If there is **no active semester**, you get a `no_active_semester` error.
   - Only **one request per subject per semester** is allowed; a second attempt reports
     `already_sent`.
2. Students open `GET /feedback/{token}` (a **public, no-login** page), submit once, and the
   token is consumed. See the student side in [student-journey.md](student-journey.md).
3. `GET /teacher/subjects/{id}/feedback` shows the collected responses for the current
   semester and the **average rating**.
4. `GET /teacher/subjects/{id}/feedback/export.csv` downloads all responses (with student
   name/email, rating, and the three text fields).

**Permission.** Owner/ADMIN for the request, view, and export.

---

## 10. Notifications

### 10.1 In-app notifications

`GET /notifications` lists your latest 50 notifications (newest first). Mark one read
(`POST /notifications/{id}/read`), mark all read (`POST /notifications/read-all`), or poll
the unread count (`GET /notifications/unread-count`). This UI is shared with students; you
only ever see your own notifications.

### 10.2 Coalesced review-digest emails

When a submission enters the "awaiting teacher review" state, an entry is queued for you in
a per-teacher digest queue (`teacher_notification_queue`). A background job groups all of a
teacher's pending entries and sends a **single digest email** instead of one email per
submission, then marks them sent so they are never re-sent. The upshot: a busy assignment
generates *one* "you have submissions to review" email, not dozens. You don't configure
anything for this — it happens automatically.

---

## Account management (ADMINs only)

Admins sit above teachers and manage the platform. These pages require the ADMIN role.

- `GET /admin` — system dashboard: user counts by role, outbox (email/job) status, and the
  20 most recent audit-log entries.
- `GET /admin/users` — list every account; `POST /admin/users/{id}/toggle-active` activates
  or deactivates an account (you cannot deactivate your own).
- `GET /admin/teachers/create` → `POST /admin/teachers/create` — **create teacher
  accounts** (username + password ≥ 8 chars). This is how every teacher gets in.
- `GET /admin/audit` — the immutable audit log (up to 200 recent administrative actions:
  enrollments, submission decisions, account changes, etc.).

If you need a teacher account, a new subject owner reassigned, or an account reactivated,
this is the section an admin uses.

---

## Appendix: what you cannot do (current limitations)

So you don't go looking for features that aren't there:

- **No in-portal subject/assignment editor.** All subject structure comes from the config
  ZIP (§3). The legacy on-screen Subject/Assignment CRUD forms were removed.
- **No GitHub/GitLab ingestion.** Submissions are **ZIP uploads only**; the old
  GitHub-PR / Google-Forms pipeline has been retired (see git history before 2026-06-19).
- **No per-teacher aggregate analytics yet.** The overview and fraud dashboards are ADMIN
  only (§8).
- **No manual numeric grade entry on the review screen.** Review actions are
  approve/reject; the numeric grade comes from the automated check and the config's grade
  range.
</content>
</invoke>
