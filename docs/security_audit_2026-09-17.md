# Security Audit — 2026-09-17

Scope: full codebase, git history, locked dependencies, Docker/Caddy deployment, CI.
Tools run locally: `gitleaks` (167 commits), `pip-audit` (runtime lock), `bandit -ll`,
`trivy fs` (vuln + secret + misconfig). Everything else is a manual read of the routes,
config, sandbox, storage and templates.

Tag per finding: **DIRECT** = fixed in this pass, no data/schema/auth impact.
**NEEDS-OK** = touches persistence, auth/session, schema or requires rotation; waits for
the owner's go-ahead.

Baseline that is already right (not findings): bcrypt(12) hashing; JWT in an
HttpOnly + SameSite=Strict + Secure cookie; every route carries a role dependency; object
authorisation on every ID (`require_subject_access` for teachers, `student_id` scoping for
students, `user_id` scoping for notifications) — no IDOR found; `Sec-Fetch-Site`/Origin
CSRF middleware; `SECRET_KEY` from env with `min_length=32` and a placeholder denylist;
`DEBUG=false` and `ENVIRONMENT=production` pinned in `docker-compose.prod.yml`; Jinja
autoescape on (`select_autoescape`), no `|safe`, no `innerHTML` with user data; all SQL
through the ORM (the only `text()` calls are advisory locks and a JSONB join with no user
input); `yaml.safe_load` everywhere; no `eval`/`pickle`/`shell=True`; Zip Slip + bomb
guards in `safe_zip.py`; sandbox `--network none --read-only --pids-limit`; proctoring
frames private (no ACL, served only via an authorised route); Caddy sets HSTS,
nosniff, X-Frame-Options, Referrer-Policy and hides `/metrics`; prod image runs as
uid 10001; tests use testcontainers Postgres (unit/integration/functional) and a
dedicated `postgres-e2e` service — no path to a production DSN.

Git history: `gitleaks` found 7 hits, all test fixtures or documented placeholders
(`STRONG_KEY` in unit tests, `tok-valid-123`, the e2e `SECRET_KEY` example in a plan
doc, empty `GRAFANA_CLOUD_PROM_TOKEN=` lines). `.env` has never been committed. **No real
secret is in the history; nothing needs rotating on that account.**

---

## HIGH

### H1. Vulnerable runtime dependencies — DIRECT
`pip-audit` on the frozen lock:

| package | locked | advisories | fixed in |
|---|---|---|---|
| starlette | 0.52.1 | PYSEC-2026-161, -248, -249, -2280, -2281 | 1.3.1 |
| python-multipart | 0.0.22 | PYSEC-2026-3036…3040 (multipart parsing) | 0.0.31 |
| cryptography | 46.0.5 | GHSA-537c-gmf6-5ccf | 48.0.1 |
| ecdsa (via python-jose) | 0.19.1 | PYSEC-2026-1325, -2467 | 0.19.2 |
| pyasn1 | 0.6.2 | PYSEC-2026-2263, -3455…3457 | 0.6.4 |
| pydantic-settings | 2.13.1 | CVE-2026-58203 | 2.14.2 |
| python-dotenv | 1.2.1 | PYSEC-2026-2270 | 1.2.2 |

Why: starlette and python-multipart are the request parser — form/upload DoS and
parser bugs are reachable by any student before auth. Fix: refresh `uv.lock`, run the
full suite, and let the new CI job fail the build on the next one.

### H2. Plaintext student passwords retained forever in `outbox_messages.payload` — NEEDS-OK
`teacher_portal.import_students`, `add_student`, `resend_credentials` enqueue
`SEND_CREDENTIALS` with `payload.password` in clear. `send_credentials_tasks.py`
documents "transmitted once, then discarded from outbox" but nothing removes it: every
finished row keeps the initial password. A DB dump or read-only DB access yields working
credentials for every student who never changed their password.

Proposed fix (two parts):
1. App: after a successful send, replace `payload["password"]` with `"<sent>"` before the
   row is marked finished (same transaction). Also keep it out of the retry path.
2. Backfill: an Alembic **data** migration `UPDATE outbox_messages SET payload =
   payload - 'password' WHERE event_type='SEND_CREDENTIALS' AND state='FINISHED'`.
   Additive, no schema change, but it modifies production rows → you run it.

### H3. `subjectCode` unvalidated → path traversal on config apply — DIRECT
`ConfigApplyService` uses `plugins_dir / subject_code` for the extracted tree and
`os.replace()`s whatever is there. `subjectCode: "../templates"` from a teacher's ZIP
replaces `/app/templates` inside the container with attacker-controlled Jinja files →
server-side template injection → code execution as the app user, which holds the Docker
socket. Teachers are semi-trusted, but a teacher account is one phished password away.
Fix: accept only `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$` with no `..`, reject at apply time.
`check_tasks` builds the same path from the stored config; same guard applied there.

---

## MEDIUM

### M1. Quiz `/event` body is unvalidated — DIRECT
`report_violation` does `data = await request.json()` then `data.get("type")`. A JSON
array → `AttributeError` → 500. `type` can be any string of any length and each distinct
value becomes a new key in the JSONB `violations` column; nothing bounds key count or
body size. Fix: parse with a Pydantic model (`type: str`, 1–64 chars, `[a-z0-9_]`),
cap distinct event keys per attempt, ignore unknown types with a 200 so the client never
retries.

### M2. Proctoring snapshot upload trusts the client — DIRECT
Content-type comes from the multipart header; bytes are never checked; no cap on frames
per attempt (2 MB × unlimited → object-storage DoS by one student). Fix: sniff JPEG/PNG/
WebP magic bytes, cap frames per attempt (200), 413 above.

### M3. CSV exports allow formula injection — DIRECT
`export_grades_csv` and `export_feedback_csv` write names, e-mails and free-text feedback
raw. A student's `went_well` of `=HYPERLINK(...)` or `=cmd|...` executes when the
teacher opens the sheet in Excel/LibreOffice. Fix: prefix any cell starting with
`= + - @ \t \r` with `'`.

### M4. Sessions survive password change / reset — NEEDS-OK
The JWT is stateless (8 h) and `_get_current_user` only checks `is_active`. After a
password reset the old cookie keeps working — the reset flow is exactly when a
compromised session should die. Proposed: nullable `users.password_changed_at`
(additive migration), set it on change/reset, add `iat` to the token, reject tokens
issued before it. Existing tokens without `iat` remain valid until they expire.

### M5. Reset and feedback tokens stored in clear — NEEDS-OK (known, deferred 2026-06)
`password_reset_tokens.token`, `feedback_tokens.token` are the bearer secrets. A DB
read gives a reset link for any pending user. Proposed: additive `token_hash` column,
write SHA-256, look up by hash, keep reading the plaintext column for rows created
before the deploy, then stop populating `token` (no drop).

### M6. `subject_test_students.plain_password` — NEEDS-OK (known, deferred 2026-06)
Stored and rendered on the subject page. "Enter as test student" already mints a JWT
directly, so the password is only informational. Proposed: stop writing it, stop
rendering it, make the column nullable (additive ALTER). Column stays.

### M7. Tailwind is loaded from `cdn.tailwindcss.com` — NEEDS-OK (scope)
A third-party script on every page, including the quiz. Compromise or outage of that
CDN is a compromise or outage of the app, and it forces `'unsafe-inline'` styles so a
real CSP is impossible. Fix is a build step (Tailwind CLI → `static/app.css`, vendored
like the MediaPipe assets). Separate change; flagged here.

### M8. No app-level security headers, no CSP — DIRECT
Headers live only in the Caddyfile, so dev/e2e and any future deployment without Caddy
run naked. Fix: `SecurityHeadersMiddleware` setting `Content-Security-Policy:
default-src 'self' https://cdn.tailwindcss.com; script-src 'self' 'unsafe-inline'
https://cdn.tailwindcss.com; style-src 'self' 'unsafe-inline'; img-src 'self' data:
blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'self';
base-uri 'self'; form-action 'self'; object-src 'none'`, `Permissions-Policy:
camera=(self), geolocation=(self), microphone=(), payment=()`, plus nosniff /
X-Frame-Options / Referrer-Policy (idempotent with Caddy). `'unsafe-inline'` stays until
M7 lands; the CSP still kills clickjacking, base hijacks, form exfiltration and plugin
objects.

### M9. Login with a >72-byte password crashes — NEEDS-OK (auth path)
bcrypt 5 raises `ValueError` on >72 bytes; `verify_password` does not catch it → 500,
and the failure is never recorded by the limiter (an unthrottled request path).
Proposed: treat >72-byte passwords as a failed login (record + 401), and reject them at
set time with the normal 422.

### M10. Login throttle is per (IP, username) only — NEEDS-OK (auth path)
One IP can try one password against every username unthrottled (password spraying).
Proposed: add a second, per-IP budget (e.g. 50 failures / 15 min) on `/auth/login`.

### M11. Sandbox: no capability drop — DIRECT
`docker run` sets network/none, read-only, pids and memory limits but keeps the default
capability set and allows privilege escalation. Fix: `--cap-drop=ALL
--security-opt=no-new-privileges`. Architectural note (no fix here): the app container
mounts `/var/run/docker.sock`, so any code execution in the app is root on the host.
Long-term: a rootless/remote daemon or a sandbox broker sidecar.

### M12. Student e-mails in structured logs — DIRECT
`send_credentials_sent`, `feedback_request_email_sent`, `*_no_channel`,
`deadline_reminder_sent` log `student_email`. Logs ship to Grafana Cloud. Fix: log
`student_id`/`token_id` instead.

---

## LOW

### L1. OpenAPI UI exposed in production — DIRECT
`/docs`, `/redoc`, `/openapi.json` are on. Public repo, so not a secret, but they enumerate
every route with parameters. Off unless `is_development`.

### L2. `DEBUG=true` is accepted alongside `ENVIRONMENT=production` — DIRECT
FastAPI debug returns tracebacks. Add a settings validator that refuses the combination.

### L3. Username enumeration by timing on login — NEEDS-OK (auth path)
bcrypt runs only when the user exists. Proposed: verify against a dummy hash when it
does not.

### L4. CORS: `allow_methods=*`, `allow_headers=*`, credentials on — informational
Origins are restricted to the app's own domain in prod, so this is inert. Left as is.

### L5. `.env.example` carries a real Grafana Cloud stack URL — DIRECT
`GRAFANA_URL=https://charmingaphid2632.grafana.net`. Replace with a placeholder.

### L6. Runner image runs as root (trivy DS-0002) — informational
It needs the host Docker socket anyway; a non-root user changes little. Not changed.

### L7. Teacher-supplied anti-cheat `message` is a `str.format` template — informational
Only ints are passed, so `{count.__class__}` is the worst case; no code execution.

### L8. Local developer `.env` contains live OpenAI and GitHub keys — informational
Gitignored and never committed (verified with `git log --all -- .env`). Trivy flags it
because it scanned the working tree. Nothing to rotate; just be aware it is on disk.

### L9. bandit: `chmod 0o777` on the ephemeral sandbox output dir — accepted
Required for non-root subject images to write `result.json`; the dir lives for one run.

---

## CI additions (this pass)

New `.github/workflows/security.yml` (PR, push to main, weekly): `pip-audit` on the
lock, `bandit -ll`, `gitleaks` over full history, `trivy fs` (vuln+secret+misconfig)
and `trivy image` on the built app image, CodeQL (python + actions), `zizmor` on the
workflows. HIGH/CRITICAL fail the job; `app-image` publishes only when it passes.
`.github/dependabot.yml` for pip + GitHub Actions + Docker base images, weekly.
