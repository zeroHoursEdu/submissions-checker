## Why

The quiz camera-proctoring recording-consent notice (`GET /portal/consent`) is the one hardcoded
English string left in an otherwise fully Ukrainian UI — every other string on that same page
(`vocab.consent.page_title`, `.heading`, `.agree_button`, `.footer_note`) already comes from
`i18n/uk.yml`, but the actual legal notice body (`settings.recording_consent_notice` in
`core/config.py`) bypasses the i18n system entirely and is only ever rendered in English. A
student reads a Ukrainian page with one paragraph of English legal text in the middle.

## What Changes

- Add a Ukrainian translation of the recording-consent notice to `i18n/uk.yml` as
  `consent.notice_text`, matching how every other page string is already handled.
- `student_portal.py`'s `show_consent` route uses `settings.recording_consent_notice` when an
  operator has explicitly set it (still configurable per jurisdiction/deployment via env var,
  unchanged), and falls back to the new `vocab.consent.notice_text` otherwise. The Settings
  field's default becomes `None` instead of the hardcoded English paragraph.

## Capabilities

### Modified Capabilities
- `recording-consent`: the notice text requirement is clarified to source its default from the
  i18n vocabulary (Ukrainian) rather than a hardcoded English string, while preserving operator
  override via `RECORDING_CONSENT_NOTICE` for jurisdiction-specific legal text.

## Impact

- `src/submissions_checker/core/config.py` — `recording_consent_notice` default becomes `None`.
- `src/submissions_checker/api/routes/student_portal.py` — `show_consent` falls back to
  `vocab.consent.notice_text` when the setting is unset.
- `i18n/uk.yml` — new `consent.notice_text` key with the Ukrainian translation.
- No template changes needed — `student_consent.html` already just renders whatever `notice`
  context var it's given.
