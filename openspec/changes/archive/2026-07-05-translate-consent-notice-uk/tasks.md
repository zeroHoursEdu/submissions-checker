## 1. Ukrainian translation

- [x] 1.1 In `i18n/uk.yml`'s `consent:` block, add `notice_text` with a Ukrainian translation of the current English notice (webcam recording, snapshot storage, exam-integrity purpose, retention policy, must-agree-to-continue).

## 2. Settings + route

- [x] 2.1 In `src/submissions_checker/core/config.py`, change `recording_consent_notice`'s default from the hardcoded English string to `None` (type `str | None`).
- [x] 2.2 In `src/submissions_checker/api/routes/student_portal.py`'s `show_consent`, change the `notice` context value to `settings.recording_consent_notice or vocab["consent"]["notice_text"]` (or however vocab is accessed in that route — match the existing pattern used for other `vocab.*` lookups in the file).

## 3. Verification

- [x] 3.1 Add/extend a functional test asserting `GET /portal/consent` renders the Ukrainian notice text by default, and a separate test asserting that when `RECORDING_CONSENT_NOTICE` env var / settings override is set, that custom text is shown instead.
- [x] 3.2 Run the functional test suite for consent/student_portal to confirm no regression.
