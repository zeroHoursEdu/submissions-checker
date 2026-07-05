## 1. Route

- [x] 1.1 In `src/submissions_checker/api/routes/student_portal.py`, change `notification_preferences_page`'s route decorator from `@router.get("/notification-preferences", ...)` to `@router.get("/settings", ...)`.
- [x] 1.2 Update `toggle_notification_preference`'s redirect target from `/portal/notification-preferences` to `/portal/settings`.
- [x] 1.3 Update the `render(...)` call's template name from `student_notification_preferences.html` to `student_settings.html`.

## 2. Template

- [x] 2.1 Rename `templates/student_notification_preferences.html` to `templates/student_settings.html`.
- [x] 2.2 Restructure its content: top-level page heading becomes "Settings" (new vocab key), with a "Notifications" section heading (existing `vocab.notifications.prefs_title`/`prefs_subtitle` repurposed for the section, not the page) wrapping the existing preference-toggle cards.
- [x] 2.3 Update the breadcrumb block to show `Portal / Settings` instead of `Portal / Notification Preferences`.

## 3. Nav + vocab

- [x] 3.1 In `templates/base.html`, change the student nav link from `/portal/notification-preferences` / `vocab.nav.notification_prefs_link` to `/portal/settings` / a new `vocab.nav.settings_link`.
- [x] 3.2 In `i18n/uk.yml`, add `nav.settings_link` ("Налаштування" or similar) and a settings-page-title vocab key; keep/repurpose the existing `notifications.prefs_title`/`prefs_subtitle` as the Notifications section's heading/subtitle within the new page.

## 4. Tests

- [x] 4.1 Update `tests/functional/test_student_portal.py` and `tests/functional/test_coverage_gaps.py`: replace GET-page assertions against `/portal/notification-preferences` with `/portal/settings` (the auth-gate parametrized test, `test_notification_prefs_page_ok`, and any redirect-target assertions after a toggle). Leave the toggle POST endpoint's own path unchanged in these tests.
- [x] 4.2 Run the functional test suite for student_portal to confirm no regression.
