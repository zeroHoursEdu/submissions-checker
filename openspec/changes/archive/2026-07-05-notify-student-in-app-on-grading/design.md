## Context

`StudentAssignment.grade` is a nullable column described as "holds final grade" but is never
actually written anywhere in the codebase today — no route or task sets it. "Receiving a grade"
in the everyday sense this feature targets is really "the submission's outcome becomes known and
final": either the automated checks resolve directly (`tests_only` mode: pass or fail, no further
step), or a teacher explicitly approves/rejects. Both already exist as code paths; neither
consistently notifies the student in-app.

`push_notification(db, user_id, title, body, link=None)` (`services/notification_service.py`)
already does the insert; it just needs a `user_id`, resolved from `Student.id` via
`User.student_id == student_id` (the `Notification.user_id` FK points at `users.id`, not
`students.id`).

## Goals / Non-Goals

**Goals:** every submission that reaches a final, no-further-review outcome pushes an in-app
notification the student can see via the existing bell/feed.

**Non-Goals:** implementing actual numeric grade computation/population for
`StudentAssignment.grade` — genuinely out of scope; the ask is about telling the student their
result is in, not about deciding how a percentage becomes a letter grade or similar. Also not
touching `tests_then_ai`/`tests_then_teacher`/`tests_then_ai_then_teacher`/`tests_then_quiz` —
each of those has its own eventual resolution point (AI review completion, teacher review, quiz
scoring) that would need its own notification wiring; bundling all of them into one change grows
scope well past what a single reviewable change should cover.

## Decisions

**Resolve `user_id` inline at each call site via a small helper, not a new join everywhere.**
`check_tasks.py` doesn't currently load the `Student`/`User` rows (only `StudentAssignment`,
`SubjectsAssignment`, `Subject`) — add one `select(User.id).where(User.student_id == ...)` query
at the point of transition rather than restructuring the existing `selectinload` chain.

**Notification body includes the assignment title and pass/fail (or approve/reject) outcome, with
a link back to the assignment page.** Matches the existing email templates' level of detail
(`submission_reviewed_template`) so the two channels are consistent.

**Push the notification unconditionally — not gated by the existing `NotificationPreference`
(email-only) system.** That preference model is scoped to `(case, method=EMAIL)`; there is no
`method=IN_APP` today, and conflating "student turned off grading emails" with "student can't see
their own bell notifications" would be a surprising, unrequested behavior change. In-app
notifications are a different channel with different expectations (low-friction, always-on,
easily dismissed) than email.

## Risks / Trade-offs

- [Risk] Adding a query per submission-completion adds minor overhead. → Mitigation: single
  indexed lookup (`users.student_id` is a FK, effectively unique), negligible next to the
  Docker sandbox run that already dominates this code path's latency.
