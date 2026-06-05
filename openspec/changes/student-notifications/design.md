## Context

The platform already dispatches email notifications through an outbox/worker pattern (`OutboxMessage` → `execute_submission_reviewed_task`). Students currently cannot opt in or out; every submission check triggers an email. The goal is to add per-student, per-case, per-method preference rows that the notification tasks consult before sending.

Existing relevant pieces:
- `notification_tasks.py` — outbox handlers, currently unconditional
- `services/notifications/dispatcher.py` — email dispatch abstraction
- `db/models/notification.py` — in-app bell notifications (unrelated to email prefs)
- Enums live in `db/models/enums.py`

## Goals / Non-Goals

**Goals:**
- Persist per-student opt-in/out flags for each (notification_case, method) pair
- Default to email ON for `SUBMISSION_CHECKED` when no preference row exists (opt-out model)
- Student portal UI page to view and toggle preferences
- Notification tasks check preference before dispatching email

**Non-Goals:**
- Adding new notification cases beyond `SUBMISSION_CHECKED` (future work)
- Notification methods beyond `EMAIL` (future work)
- Teacher/admin notification preferences
- Push notifications, SMS, or webhooks

## Decisions

### 1. Separate `notification_preferences` table (not a JSON column on `students`)

A dedicated table with `(student_id, case, method)` composite unique key lets future cases/methods be added without schema migration on the student row. Querying a single preference is a direct indexed lookup.

*Alternatives considered*: JSON column on `students` — simpler schema, but requires parsing on every check and makes future indexed lookups harder.

### 2. Opt-out model with lazy seeding

If no preference row exists for a (student, case, method), the system treats it as **enabled** (preserving current behavior). Rows are only written when a student explicitly saves preferences. This avoids a migration-time bulk-insert and keeps the model simple.

*Alternatives considered*: Seed all students on migration — guarantees rows always exist, but adds complexity and bulk data to every new student onboarding.

### 3. New `NotificationCase` and `NotificationMethod` enums in `enums.py`

Keeps enum definitions co-located with other domain enums, consistent with project conventions.

### 4. Gate added to existing outbox tasks (not a new middleware layer)

The preference check is a direct DB query inside `execute_submission_reviewed_task`. Simple, no new abstractions needed for a single case.

### 5. Toggle endpoint via POST form (not JSON API)

The UI is server-rendered Jinja2; a form POST returning a redirect keeps the pattern consistent with all other student portal actions.

## Risks / Trade-offs

- **N+1 on bulk dispatch**: Each outbox task checks one student's preference with a single PK lookup — no fan-out risk for now.
- **Stale default assumption**: If the opt-out model assumption ever inverts, existing students without rows would silently get emails again. → Mitigation: document the default clearly; revisit if requirements change.
- **Migration ordering**: New table must exist before any preference rows are inserted. A single Alembic migration is sufficient.

## Migration Plan

1. Add Alembic migration creating `notification_preferences` table
2. Deploy app — new table exists, tasks check preference (default: enabled for missing rows)
3. No rollback risk: removing the preference check reverts to old unconditional behavior
