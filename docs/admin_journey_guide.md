# Admin Journey — Full Guide

This is the end-to-end guide for **administrators** of submissions-checker. Like the
[teacher](teacher_journey_guide.md) and [student](student_journey_guide.md) guides, it is
written for the people who use the system, not for engineers: each capability explains
*what it does*, *where to find it*, *what you need to provide*, *what gets recorded*, and
*what happens as a result*.

An admin sits **above** teachers. Everything a teacher can do, you can do too — on *any*
subject, not just ones you own — **plus** the account-management and platform-wide
reporting that teachers cannot reach. If you have not yet read the
[Teacher Journey guide](teacher_journey_guide.md), read it first: this guide does not
repeat the subject / enrollment / review features that you inherit from the teacher role,
it only documents the admin-exclusive surface and the places where being an admin changes
the rules.

---

## Before you start: how the admin role works

The system has three account roles. Yours is the most privileged.

| Role | What it can do |
|---|---|
| **STUDENT** | The student portal only — see [student_journey_guide.md](student_journey_guide.md). |
| **TEACHER** | Subject-scoped teaching features, but only on subjects they own — see [teacher_journey_guide.md](teacher_journey_guide.md). |
| **ADMIN** | Everything a teacher can do on **any** subject, **plus** account management (create teachers, activate/deactivate accounts, read the audit log) **plus** the semester calendar. |

### Two privileges that are unique to ADMIN

1. **Object-level authorization bypass.** Teacher pages enforce an *ownership* rule:
   a teacher only sees and acts on subjects whose `Subject.owner_id` is their own account
   (the `require_subject_access` helper returns **403 "Not authorized for this subject"**
   otherwise). **ADMIN bypasses this check on every subject-scoped page.** You can open,
   review, enroll into, export, and delete *any* teacher's subject and *any* student's
   profile, regardless of who owns it. This is intentional — an admin is the platform-wide
   operator — but it means you should treat that power carefully.

2. **Platform-wide configuration.** Teacher accounts and the semester calendar (which gates
   course feedback) are created and edited only here.

### How role checks are enforced

Every admin page depends on the `AdminUser` guard (`_require_admin` in
`api/dependencies.py`). It first authenticates you from the session cookie, then checks
`role == ADMIN`; anyone who is not an admin gets **403 "Admin access required"**.

> **There is no public admin sign-up**, just as there is none for teachers. Admin accounts
> are seeded directly in the database / at deployment. From an admin account you create the
> *teacher* accounts; teachers in turn create *student* accounts.

---

## Admin feature catalogue (quick reference)

| # | Capability | Page / route | Inputs | Permission | What gets audited | Outcome |
|---|------------|--------------|--------|------------|-------------------|---------|
| 1 | Admin dashboard | `GET /admin` | — | ADMIN | — | User counts by role, email/job outbox status, 20 most recent audit entries |
| 2 | List all users | `GET /admin/users` | — | ADMIN | — | Every account, ordered by role then username |
| 3 | Activate / deactivate a user | `POST /admin/users/{id}/toggle-active` | — | ADMIN | `toggle_user_active` (target user id + new state) | Account's `is_active` flips; inactive accounts can no longer log in |
| 4 | Create teacher account | `GET` then `POST /admin/teachers/create` | username + password (≥ 8 chars) | ADMIN | `create_teacher` (new username) | A new TEACHER account is created |
| 5 | Read the audit log | `GET /admin/audit` | — | ADMIN | — | Up to 200 most recent recorded actions, newest first |
| 6 | Semesters | `GET /admin/semesters`, `POST /admin/semesters`, `POST /admin/semesters/{id}` | name, season, start/end dates | ADMIN | `create_semester`, `update_semester` | Calendar that gates course feedback; overlaps refused |
| — | *All teacher features* | `…/teacher/*` etc. | as per the teacher guide | inherited, on **any** subject | as per teacher guide | object-level ownership check is bypassed for you |

---

## 1. The admin dashboard

**What it does.** Your home screen — `GET /admin`. It is a system health and activity
overview, not a teaching surface.

**What you see.**

- **User counts by role** — how many ADMIN, TEACHER and STUDENT accounts exist
  (`User.role` grouped and counted).
- **Outbox status** — a breakdown of the asynchronous message/job queue
  (`OutboxMessage.state`) by state: `PENDING`, `FINISHED`, `ERROR`. This is your quick
  signal that emails (credentials, review notices, feedback requests) and background jobs
  are flowing — a growing `ERROR` count means something needs attention.
- **The 20 most recent audit-log entries** (newest first) — a live feed of who did what.

**Permission.** ADMIN only. **Audited:** nothing — viewing the dashboard is not recorded.

---

## 2. Listing all users

**What it does.** `GET /admin/users` lists **every** account on the platform — admins,
teachers and students alike — ordered by role then username.

**Why you use it.** It is the entry point for activating or deactivating accounts (§3) and
for confirming a teacher account you created actually exists.

**Permission.** ADMIN only. **Audited:** nothing for viewing.

---

## 3. Activating and deactivating accounts

**What it does.** `POST /admin/users/{id}/toggle-active` flips an account's `is_active`
flag. This is the platform's "disable login" switch and works on any account — teacher or
student.

**Inputs.** None beyond the target user id in the URL (the button is on the users list).

**Rules.**

- If the user id does not exist you get a **404**.
- **You cannot deactivate your own account** — attempting it returns
  **400 "Cannot deactivate your own account"**. This stops you locking yourself out.
- Otherwise the flag simply toggles: an active account becomes inactive and vice versa.

**What happens to a deactivated account.** They can no longer authenticate — the
`_get_current_user` dependency rejects inactive users with **401 "User inactive"** on the
next request, so any existing session is effectively dead too.

**Audited.** Yes — a `toggle_user_active` audit entry is written recording the actor
(you), the **target user id**, and the **new `is_active` value**. **Outcome:** redirect
back to `/admin/users`.

**Permission.** ADMIN only.

---

## 4. Creating teacher accounts

**What it does.** This is *the* way every teacher gets into the system — there is no
self-service teacher sign-up.

**How.**

1. `GET /admin/teachers/create` opens the creation form.
2. `POST /admin/teachers/create` with a **username** and a **password**.

**Rules / validation.**

- The **password must be at least 8 characters** — otherwise the form re-renders with
  *"Password must be at least 8 characters."* (HTTP 422).
- The **username must be unique** — a clash re-renders with *"Username already taken."*
  (HTTP 422).
- On success the password is hashed with **bcrypt** (work factor 12) — the plaintext is
  never stored — and a new `User` with role `TEACHER` is created.

**Audited.** Yes — a `create_teacher` entry recording the actor and the **new username**.
**Outcome:** redirect to `/admin/users`, where the new teacher now appears. Communicate the
chosen credentials to the teacher out-of-band; this flow does not email them.

**Permission.** ADMIN only.

---

## 5. The audit log

**What it does.** `GET /admin/audit` shows the **immutable** record of important actions
across the platform — up to the **200 most recent**, newest first. (The dashboard in §1
shows only the latest 20; this page is the fuller history.)

**What an entry contains** (`audit_logs` table / `AuditLog` model):

| Field | Meaning |
|---|---|
| `created_at` | When the action happened (indexed). |
| `actor_id`, `actor_username` | Who did it (may be empty for system actions). |
| `action` | A short verb-string identifying the action. |
| `target_type`, `target_id` | What the action was about, when applicable. |
| `detail` | A free-form JSONB blob with action-specific context. |

**What is actually recorded today.** The codebase writes audit entries for these actions
(the `audit()` helper appends a row but never commits on its own — the calling action owns
the transaction, so an audited action and its audit entry succeed or fail together):

| `action` value | Written when | Source |
|---|---|---|
| `create_teacher` | An admin creates a teacher account (§4). | admin route |
| `toggle_user_active` | An admin activates/deactivates an account (§3). | admin route |
| `add_student` | A teacher/admin adds a single student account. | teacher route |
| `enroll_student` | A student is enrolled into a subject. | teacher route |
| `unenroll_student` | A student is removed from a subject. | teacher route |
| `teacher_approve_submission` / `teacher_reject_submission` | A submission is approved or rejected on review. | teacher route |
| `student_submit` | A student uploads a submission. | student route |

**Important — the log is append-only.** There is no edit or delete endpoint; the model has
no update path. Treat it as evidence.

**Permission.** ADMIN only. **Audited:** nothing for viewing the log itself.

---

## 6. Semesters

`GET /admin/semesters` lists every semester with its season and dates and marks the current
one. Course feedback (`POST /teacher/subjects/{id}/feedback/request`) is keyed by the
semester whose dates contain today, so a term outside the seeded calendar needs a row here
first. `POST /admin/semesters` adds one, `POST /admin/semesters/{id}` edits one (inline form
per row). The end date must be after the start date and periods may not overlap; both are
refused with a message. Every change is audited (`create_semester`, `update_semester`).

---

## 7. Where the numbers live now

The in-app analytics pages were removed in favour of two things that already exist:

- **Per subject, in the app:** the teacher's **Панель** tab shows the cached stat cards
  (average mark, pass %, pending review, cheating %), the assignment board shows integrity
  flags, AI-review badges and evidence thumbnails, the review page shows the full AI verdict
  and every proctoring frame, and **Звіт схожості** lists who matches whom. As ADMIN you see
  all of this on every subject.
- **Platform-wide, in Grafana:** request, outbox, check, AI-review and email counters plus
  DB-derived gauges (`docs/observability.md`).

## 8. Everything a teacher can do — on any subject

Because ADMIN passes every `require_subject_access` / ownership check, you inherit the full
teacher surface from [teacher_journey_guide.md](teacher_journey_guide.md) **without the
ownership restriction**. In practice this means you can, for *any* teacher's subject:

- upload/update a subject config ZIP, view a subject, and **soft-delete** subjects
  (note: subject *delete* is owner-checked in the teacher flow, but ADMIN satisfies the
  access guard);
- import/enroll/unenroll students and browse the global roster;
- open the assignment review board, review a submission, and **approve/reject** it
  (these write `teacher_approve_submission` / `teacher_reject_submission` audit entries and
  email the student);
- export grades, request/view/export course feedback;
- receive in-app notifications and the coalesced review-digest emails.

Refer to the teacher guide for the per-feature detail — the behaviour is identical; only
the *scope* (any subject vs. owned subjects) differs.

> **One nuance:** the **test-student** endpoints in the teacher flow are gated to the
> subject **owner only**, not merely "subject access". Provisioning or entering as a test
> student is therefore the owner's action; as an admin you have the run of everything else.

---

## Appendix: what an admin cannot do (current limitations)

So you don't go looking for features that aren't there:

- **No per-teacher analytics scoping yet.** The overview and fraud dashboards are
  all-or-nothing platform-wide views, ADMIN only (§6) — there is a tracked `TODO security`
  to scope them per owner.
- **No audit-log editing.** The log is append-only by design; there is no endpoint to
  modify or delete entries (§5).
- **No emailed teacher credentials.** Creating a teacher (§4) does not send an email —
  hand over the username/password directly.
- **No self-deactivation.** You cannot toggle your own account inactive (§3).
- **No GitHub/GitLab ingestion anywhere on the platform.** Submissions are **ZIP uploads
  only**; the historical GitHub-PR / GitLab-MR ingest paths have been retired (the enum
  members survive only to avoid a destructive database migration; no code produces them).
