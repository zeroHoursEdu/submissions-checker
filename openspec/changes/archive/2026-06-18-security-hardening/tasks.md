# Tasks — Security Hardening

## 1. Config / secrets
- [x] 1.1 Reject placeholder/weak `SECRET_KEY` at startup (validator)
- [x] 1.2 Add `Settings.cookie_secure` (True in production)
- [x] 1.3 Remove stale ngrok `base_url` default

## 2. Auth / session
- [x] 2.1 DB-backed `_get_current_user`: 401 if user missing or `is_active` false; take role/username from DB
- [x] 2.2 Set auth cookie `secure` from `settings.cookie_secure`

## 3. Webhook / RCE-SSRF
- [x] 3.1 Verify GitHub HMAC signature; 401 on failure before parsing
- [x] 3.2 Validate clone URL is https GitHub; harden `git clone` transports

## 4. Input safety (ZIP)
- [x] 4.1 New `utils/safe_zip.py`: Zip-Slip + total-size + entry-count guards
- [x] 4.2 Use `safe_extract` in `config_apply.py` and `check_tasks.py`

## 5. Object-level authorization (RBAC)
- [x] 5.1 New `api/authz.py`: `require_subject_access` helper
- [x] 5.2 Apply ownership check to all subject-scoped teacher routes
- [x] 5.3 Enforce ownership on submission review + enroll/unenroll
- [x] 5.4 Scope analytics to owned subjects (or ADMIN)
- [x] 5.5 Open-redirect guard in `i18n.set_language`
- [x] 5.6 Test-student cookie `secure` from settings

## 6. Verify
- [x] 6.1 Imports resolve; app boots; targeted tests pass

## Follow-ups (documented, out of this batch)
- [ ] Hash reset/feedback bearer tokens at rest (migration)
- [ ] Remove plaintext `subject_test_students.plain_password` (migration)
- [ ] CSRF tokens + login rate limiting
