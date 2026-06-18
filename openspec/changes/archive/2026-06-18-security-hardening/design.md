# Design — Security Hardening

## Decisions

### 1. Reject weak `SECRET_KEY` at startup (fail-fast)
A `min_length=32` validator is insufficient — the shipped placeholder passes
it. Add a pydantic validator that rejects a small denylist of known
placeholders and, in `production`, any obviously-weak value. App refuses to
boot rather than silently signing forgeable tokens.

### 2. `cookie_secure` derived from environment
Add `Settings.cookie_secure` → `True` in production, `False` otherwise (local
HTTP dev still works). All cookie-setting call sites read this single property
instead of hardcoding `secure=False`. One source of truth.

### 3. Webhook authentication is mandatory
`handle_github_webhook` calls `verify_github_signature(body, header)` and
returns `401` on failure, before any payload parsing. Defense in depth: the
clone URL is validated to be an `https://` GitHub URL, and `git clone` runs
with `-c protocol.ext.allow=never -c protocol.fd.allow=never` (and an allowlist
of safe transports) so a malicious URL cannot spawn a shell.

### 4. `safe_extract` utility (`utils/safe_zip.py`)
Single hardened extractor used by both ZIP paths:
- reject entries whose resolved path escapes the destination (Zip Slip)
- reject absolute paths and symlinks
- cap total uncompressed size and entry count (zip-bomb guard)
Replaces every `ZipFile.extractall`.

### 5. Subject-ownership authorization helper
`require_subject_access(db, subject_id, current_user)` loads the subject and
raises `403` unless `subject.owner_id == current_user.user_id` or the user is
`ADMIN`; returns the subject so handlers reuse the query. Applied to every
subject-scoped teacher route and to analytics (scoped to owned subjects, or
ADMIN). This is the core RBAC gap closure.

### 6. Live session validation
`_get_current_user` becomes DB-backed: after decoding the JWT it loads the
`User`, and raises `401` if the user is missing or `is_active` is false. Cost
is one indexed PK lookup per request — acceptable, and it makes
deactivation/deletion effective immediately. Role/username are taken from the
DB row, not trusted from the token claim.

### 7. Token hashing at rest
`password_reset_tokens.token` and `feedback_tokens.token` store a SHA-256 hex
digest instead of the raw token. The raw token is still emitted to the user
(email/URL); lookups hash the incoming value and compare. Migration rewrites
the column; existing unconsumed tokens are invalidated (acceptable — short TTL).

## Risks / Trade-offs
- DB lookup per request adds latency — mitigated by PK index; can add a short
  cache later if needed.
- Tightened ownership checks may surface latent UI flows that assumed
  cross-tenant access; those were bugs.
- Token-hashing migration invalidates in-flight reset links (2h TTL) — low blast radius.
- CSRF tokens and rate limiting are noted as follow-ups (broader surface);
  `SameSite=Strict` + `secure` cookies are the interim CSRF control.
