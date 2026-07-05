## 1. Data Model

- [x] 1.1 Add `NotificationCase` enum (`SUBMISSION_CHECKED`) and `NotificationMethod` enum (`EMAIL`) to `db/models/enums.py`
- [x] 1.2 Create `db/models/notification_preference.py` with `NotificationPreference` model (`student_id`, `case`, `method`, `enabled`) with composite unique index on `(student_id, case, method)`
- [x] 1.3 Export `NotificationPreference` from `db/models/__init__.py`
- [x] 1.4 Write Alembic migration `0005_add_notification_preferences.py` creating the `notification_preferences` table

## 2. Preference Check in Notification Tasks

- [x] 2.1 Add helper `get_email_enabled(db, student_id, case) -> bool` in `notification_tasks.py` that returns `True` when no row exists (opt-out default) or when `enabled = True`
- [x] 2.2 In `execute_submission_reviewed_task`, call the helper before dispatch; log `submission_reviewed_email_suppressed` and return early when disabled

## 3. Student Portal Routes

- [x] 3.1 Add `GET /portal/notification-preferences` route in `student_portal.py` — queries all preference rows for the student and renders `student_notification_preferences.html`
- [x] 3.2 Add `POST /portal/notification-preferences/{case}/{method}/toggle` route — upserts preference row (flip current state, defaulting missing rows to ON) and redirects back to preferences page

## 4. UI Template

- [x] 4.1 Create `templates/student_notification_preferences.html` extending `base.html` — renders a card per notification case with a toggle button per method showing current state (Enabled/Disabled)
- [x] 4.2 Add a "Notification Preferences" link in the student navigation (in `base.html` or `student_summary.html`)
