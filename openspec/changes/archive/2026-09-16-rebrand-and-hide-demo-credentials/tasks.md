# Tasks

## 1. Confine the credential hint to development

- [x] 1.1 Inject an `is_development` flag into the shared template context
- [x] 1.2 Render the demo-credential hint on the sign-in page only when that flag is set
- [x] 1.3 Test that a production-configured application serves a sign-in page containing no account name or password
- [x] 1.4 Test that a development-configured application still shows the hint

## 2. One product name

- [x] 2.1 Rename the brand string and the sign-in heading, renaming the i18n key with them
- [x] 2.2 Rename the product in the credentials, password-reset and notification emails
- [x] 2.3 Update the functional test that asserts the password-reset subject line
- [x] 2.4 Update remaining user-facing documentation that names the old product

## 3. Documentation

- [x] 3.1 Document the seeded local accounts in README, stating they are absent in production
- [x] 3.2 Document creating the first account directly in the database, covering bcrypt and the student-role constraint

## 4. Verification

- [x] 4.1 Lint, format, type check and the full suite pass
- [x] 4.2 Verified: a container with ENVIRONMENT=production seeds 0 users and its sign-in page contains no account name or password
- [x] 4.3 Verified: the documented INSERT produced an account that signed in (303 -> /teacher with a session cookie)
