## Context

Today: `GET /portal/notification-preferences` renders `student_notification_preferences.html`,
a page whose ENTIRE content is the preference toggles — no broader settings concept exists. The
POST toggle route (`/portal/notification-preferences/{case}/{method}/toggle`) is a separate
action endpoint already decoupled from the page's own path.

## Goals / Non-Goals

**Goals:** give students a "Settings" destination in the nav, with notification preferences as
its first (currently only) section — a natural home for any future per-student setting.

**Non-Goals:** building a multi-section settings framework, teacher-side settings, or moving the
notification bell/feed under settings — none of that was asked for, and the bell is a live inbox
(different UX category from a configurable preference) that conventionally stays top-level.

## Decisions

**Rename the GET page route, leave the POST toggle route alone.** The toggle action's path
(`/portal/notification-preferences/{case}/{method}/toggle`) has no reason to change — it's an
internal form-action URL a student never navigates to directly, and renaming it would only add
test churn for zero user-facing benefit. Only the page they land on (and its nav link) changes.

**Rename the template file to `student_settings.html`, not add a wrapper around the old one.**
The old file's content (case/method toggle cards) becomes the "Notifications" section's body
inside the new page — a straight rename + a new page heading + a section heading, not a second
template layered on top of the first.

**Keep the redirect-after-toggle target pointing at the new `/portal/settings` path.** The toggle
handler redirects back to the page it came from; since that page moved, the redirect target moves
with it.

## Risks / Trade-offs

None significant — a route/template rename with no behavior change to the preference toggle
logic itself. Existing bookmarks to the old `/portal/notification-preferences` URL would now
404, but this is an internal app with no external consumers of that specific page URL.
