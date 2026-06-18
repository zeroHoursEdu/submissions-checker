# Security Hardening

## Why

A parallel security audit of the submissions-checker found several
exploitable weaknesses. The app serves CS students — some of whom will
actively probe it — so untrusted input must be treated as hostile and
authorization must be enforced on every object, not just every role.

Highest-impact findings:

- **Auth bypass**: a placeholder `SECRET_KEY` (`your-secret-key-here-change-in-production`)
  satisfies the length validator and signs every JWT. Anyone who knows it
  forges an `ADMIN` token.
- **Unauthenticated webhook → RCE/SSRF**: `POST /webhooks/github` never
  verifies the HMAC signature; a forged payload drives `git clone` of an
  attacker-controlled URL (incl. git `ext::`/`fd::` transports = command exec).
- **Zip Slip + zip bombs**: teacher config ZIPs and student submission ZIPs
  are `extractall`-ed with no path/size validation → arbitrary file write & DoS.
- **Cross-teacher BOLA**: ~12 teacher/analytics routes check role but not
  `subject.owner_id`, so any teacher reads/mutates any other teacher's
  rosters, grades, submissions, and feedback.
- **Stale sessions**: deactivated/deleted users keep a valid JWT for 8h —
  `_get_current_user` never rechecks the DB.
- **Confidentiality**: auth cookie sent with `secure=False`; reset/feedback
  bearer tokens stored in plaintext; test-student passwords stored plaintext.

## What Changes

- Fail startup on a placeholder/known-weak `SECRET_KEY`; harden config defaults.
- Verify the GitHub webhook HMAC before processing; restrict `git` transports
  and validate clone URLs.
- Add a `safe_extract` utility (path-traversal + total-size + entry-count
  guards) and use it for both ZIP ingestion paths.
- Add a reusable subject-ownership authorization helper and apply it to all
  subject-scoped teacher routes and analytics.
- Re-validate user existence + `is_active` on every authenticated request.
- Set the auth cookie `secure` flag from environment; add an open-redirect
  guard to the language switcher.
- Hash reset/feedback bearer tokens at rest (store SHA-256, compare hashes).

## Impact

- Affected specs: `auth-security`, `subject-authorization`, `input-safety` (new).
- Affected code: `core/config.py`, `core/security.py`, `api/dependencies.py`,
  `api/routes/{auth,webhooks,teacher_portal,analytics,i18n,feedback}.py`,
  `services/config_apply.py`, `workers/tasks/{check_tasks,pull_tasks}.py`,
  `utils/git.py`, new `utils/safe_zip.py`, token models + a migration for
  token hashing.
- No breaking API changes; behavior tightens (some previously-allowed
  cross-tenant reads now return 403).
