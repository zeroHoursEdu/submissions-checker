## Why

Students currently have no control over when and how they receive notifications — emails go out on every submission check regardless of preference. Giving students visibility into notification cases and opt-in/out control reduces noise and increases trust in the platform.

## What Changes

- Add a `notification_preferences` table storing per-student, per-case, per-method enabled flags
- Seed default preferences (email ON for `SUBMISSION_CHECKED`) when no preference exists
- Before dispatching outbox notification tasks, check the student's saved preference
- Add a student portal page (`/portal/notification-preferences`) with toggles for each case and method
- Add a `NotificationCase` enum and `NotificationMethod` enum to drive both the model and the UI

## Capabilities

### New Capabilities

- `notification-preferences`: Student-owned per-case, per-method notification preference CRUD — view and toggle whether emails are sent for each notification event

### Modified Capabilities

- `notification-tasks`: Existing outbox tasks must respect student preferences before dispatching (only `SUBMISSION_CHECKED` case applies now; skip dispatch when student has disabled it)

## Impact

- **New model**: `NotificationPreference` (table `notification_preferences`) + Alembic migration
- **New enums**: `NotificationCase` (`SUBMISSION_CHECKED`), `NotificationMethod` (`EMAIL`)
- **Modified**: `notification_tasks.py` — `execute_submission_reviewed_task` checks preference before sending
- **New routes**: `GET /portal/notification-preferences`, `POST /portal/notification-preferences/{case}/{method}/toggle`
- **New template**: `templates/student_notification_preferences.html`
- No breaking changes to existing in-app notification flow or teacher-facing routes
