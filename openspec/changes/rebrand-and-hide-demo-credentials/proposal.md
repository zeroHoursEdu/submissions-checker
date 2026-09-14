# Rebrand to SubmissionChecker and stop advertising demo credentials

## Why

Two user-visible problems on the deployed system.

**The login page publishes working-looking credentials.** Every visitor to the
production sign-in page is told `Демо-акаунти — teacher / teacher123 · ivan /
student123`. The accounts themselves are not created in production — the seeding
migrations are gated behind `ENVIRONMENT=development` — so the credentials do not
work. That makes this less severe than it looks, and it is still wrong: it hands an
attacker a list of usernames to try against a real login form, and it tells students
the system was deployed without anyone reading the front page.

The hint exists to save developers typing. That is a development need, and it should
be visible only where it is needed.

**The product is named twice.** The repository, the image and the host are
`submissions-checker`; the interface, the emails and the password-reset subject line
say `EduTrack`. A student who receives "Your account credentials for EduTrack" has no
way to connect it to the site they were sent to.

## What Changes

- The demo-credential hint on the login page is shown only when the application is
  running in development.
- User-visible occurrences of `EduTrack` become `SubmissionChecker`: the login
  heading, the brand string, and the notification and password-reset emails.
- `README.md` documents which accounts exist in a local environment, what their
  passwords are, and that they are absent in production.
- The deployment documentation gains a procedure for creating the first real account
  directly in the database, which is how an operator gets into a freshly deployed
  system that has no accounts at all.

Out of scope: renaming the repository, the Docker image, or the package. Those are
`submissions-checker` already and consistent with each other.

## Impact

- Affected specs: `product-identity` (new), `local-development` (new)
- Affected code: `templates/login.html`, `i18n/uk.yml`,
  `src/submissions_checker/services/notifications/templates.py`,
  `src/submissions_checker/api/routes/auth.py`, `README.md`, `docs/deployment.md`
- One functional test asserts the old password-reset subject line and is updated
- No schema change, and no change to which accounts exist in any environment
