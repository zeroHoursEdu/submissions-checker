## Why

The student nav bar has two separate, unrelated-looking top-level links: the notification bell
(`/notifications`, the live feed/inbox) and "Notification Preferences" (`/portal/notification-preferences`,
a standalone page with no relation to any broader settings concept). There is no "Settings" page
anywhere in the app. As the only configurable-preference surface a student has, notification
preferences reads more naturally as a section within a general Settings area than as its own
top-level nav item.

## What Changes

- Rename the student's notification-preferences page from a standalone
  `/portal/notification-preferences` page to `/portal/settings`, presented as a Settings page
  with a "Notifications" section containing the existing per-case/method toggles — same
  functionality, restructured as a section within a settings page instead of the whole page.
  The toggle POST endpoints are unchanged (`/portal/notification-preferences/{case}/{method}/toggle`
  remains the action route — only the page students land on is renamed).
- The nav bar's "Notification Preferences" link becomes a "Settings" link pointing at
  `/portal/settings`.
- The notification bell/feed (`/notifications`) is unchanged — it's a live inbox, not a
  configurable preference, and stays a top-level nav item like it is in most apps.

## Capabilities

### Modified Capabilities
- `notification-preferences`: the page is now `GET /portal/settings` (a Settings page with a
  Notifications section) instead of a standalone `/portal/notification-preferences` page. Toggle
  behavior and routes are unchanged.

## Impact

- `src/submissions_checker/api/routes/student_portal.py` — `notification_preferences_page`'s
  route path changes from `/notification-preferences` to `/settings`; its redirect-after-toggle
  target updates to match.
- `templates/student_notification_preferences.html` — renamed to `templates/student_settings.html`,
  restructured with a "Settings" page heading and a "Notifications" section heading around the
  existing preference toggles.
- `templates/base.html` — nav link updated from "Notification Preferences" → "Settings",
  pointing at `/portal/settings`.
- `i18n/uk.yml` — vocab keys renamed/added to match (settings page title, notifications section
  heading, nav link label).
- Tests referencing `/portal/notification-preferences` (the GET page) update to `/portal/settings`;
  the toggle POST tests are unaffected since that route doesn't move.
