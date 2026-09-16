## Context

The platform already has subjects, enrolled students (`SubjectsStudents`), an outbox/notification pipeline for async email delivery, and a student notification-preference system. This change adds a feedback collection flow on top of that existing infrastructure.

Currently teachers have no in-platform mechanism to solicit structured end-of-semester feedback; they rely on external survey tools. Centralising feedback within the platform lets the system enforce per-student single submission and tie responses to real enrolled students without manual cross-referencing.

## Goals / Non-Goals

**Goals:**
- Teacher sends one feedback request per subject per semester (idempotent button, semester auto-derived from server date)
- Every enrolled student gets a personalised, single-use tokenised link by email (respecting their notification preferences)
- Student submits one response: 5-star rating + three hardcoded open-text questions
- Teacher views all responses with student names in a clean read-only list UI
- Teacher can export all responses for a subject to CSV
- New notification case `FEEDBACK_REQUEST` wired into existing preference opt-out system

**Non-Goals:**
- Custom question authoring
- Multiple feedback campaigns in the same semester for the same subject
- Reminder emails / automated follow-ups

## Decisions

### D1 — Tokenised single-use links over session-authenticated submission

**Decision**: Generate a unique opaque token per student per feedback request stored in `feedback_tokens`. The student's feedback form URL is `/feedback/<token>`. No login required.

**Why**: Students may receive the email days later or on a different device. Requiring login adds friction and could suppress response rate. The token itself is the authentication credential.

**Alternative considered**: Session-authenticated route under `/portal/feedback/<request_id>`. Rejected — requires the student to be logged in and places the submission behind the portal auth middleware.

---

### D2 — Semesters as a first-class table; FeedbackRequest links by FK

**Decision**: A `semesters` table is created with pre-seeded rows covering 20 semesters (Spring 2026 → Fall 2035). Each row has `id`, `name` (e.g. "Spring 2026"), `season` (SPRING | FALL), `start_date`, and `end_date`. `feedback_requests` carries a `semester_id` FK instead of a derived string, and the unique constraint is on `(subject_id, semester_id)`.

Semester date boundaries:
- **Spring**: Feb 1 – Jun 30
- **Fall**: Sep 1 – Jan 31 (of the following year)
- **Summer gap** (Jul 1 – Aug 31): no active semester — the feedback button is hidden with an explanatory note

The "current semester" is resolved at request time as `SELECT * FROM semesters WHERE start_date <= now() <= end_date`. During the summer gap the query returns no row.

**Why**: The `semesters` table becomes a reusable domain concept — future features (per-semester enrollment, assignment deadlines, grade reporting) can all FK into it. Storing a derived string on `feedback_requests` would be throwaway infrastructure.

**Alternative considered**: Store semester as a `YYYY-H1`/`YYYY-H2` VARCHAR on `feedback_requests`. Rejected — non-reusable, harder to label/display, no natural place to store season boundaries.

**Seeding**: The migration inserts 20 rows (2 per year, 2026–2035). Extending beyond 2035 requires a new migration — acceptable for a 10-year planning horizon.

---

### D3 — Feedback delivery via existing Outbox + Celery pipeline

**Decision**: Triggering a feedback request inserts one `OutboxMessage` per enrolled student (event type `FEEDBACK_REQUEST_SENT`) and lets the existing worker fan them out.

**Why**: Consistent with how `SUBMISSION_REVIEWED` emails work. Outbox gives at-least-once delivery, retries on transient SMTP failure, and decouples the HTTP request from email sending.

**Alternative considered**: Synchronous SMTP loop inside the HTTP handler. Rejected — could time out for large cohorts.

---

### D4 — Hardcoded three open-text questions

The three questions are constants in code, not stored in the DB:
1. "What went well this semester?"
2. "What didn't go well?"
3. "What would you change for next semester?"

Responses are stored in three `TEXT` columns on `feedback_responses`.

---

### D5 — Teacher view shows student identity

Responses are stored with a `student_id` FK and the teacher view displays the student's full name and email alongside their answers. Transparency is intentional: students know their identity is visible to the teacher, which discourages low-quality responses and increases accountability on both sides.

### D6 — CSV export via streaming response

`GET /teacher/subjects/{subject_id}/feedback/export.csv` returns a `text/csv` streaming response with columns: `student_name`, `student_email`, `rating`, `went_well`, `went_bad`, `to_change`, `submitted_at`. No pagination — all rows for the subject are included in one download.

## Risks / Trade-offs

- **Token guessing** → tokens are `secrets.token_urlsafe(32)` (192 bits), computationally infeasible to brute-force.
- **Large cohorts** → inserting N outbox rows in one HTTP request could be slow for very large subjects. Mitigation: wrap in a single transaction; Postgres insert of hundreds of rows is sub-second.
- **Double-click / retry of teacher button** → the unique constraint on `feedback_requests.(subject_id, semester_id)` makes the second INSERT fail; the route catches `IntegrityError` and returns a redirect with flash message.
- **Summer gap** → no row returned by current-semester query; route returns subject page with a note explaining feedback requests are only available during an active semester.
- **Student already submitted, clicks link again** → the route checks `feedback_tokens.used_at IS NOT NULL` and returns a "already submitted" page.

## Migration Plan

1. Migration `0016_add_semesters.py` creates the `semesters` table and seeds 20 rows (Spring 2026 – Fall 2035).
2. Migration `0017_add_feedback_tables.py` creates `feedback_requests`, `feedback_tokens`, `feedback_responses` with FK to `semesters`.
2. `NotificationCase` enum extended with `FEEDBACK_REQUEST` — existing preference rows are unaffected (opt-out model, missing row = enabled).
3. `OutboxEventType` enum extended with `FEEDBACK_REQUEST_SENT`.
4. No rollback complexity — all new tables; dropping them restores previous state.
