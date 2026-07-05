## Context

`settings.recording_consent_notice` docstring says "configurable for jurisdiction" — it's meant
as an operator override point (different legal text per deployment/jurisdiction), not pure UI
copy. That's distinct from ordinary i18n vocab strings, which are static per-locale translations
maintained in `i18n/*.yml`. Today it conflates both roles by hardcoding an English default.

## Goals / Non-Goals

**Goals:** make the default notice text Ukrainian (matching the rest of the UI) while preserving
the operator's ability to override it per deployment.

**Non-Goals:** building true multi-locale switching — the app only ships `i18n/uk.yml` today: no
`en.yml`, no locale-switcher. Not adding one here.

## Decisions

**Keep the settings field as an optional override, default `None`, falling back to vocab.**
Rather than deleting the field (losing jurisdiction-override capability) or keeping a hardcoded
default (losing i18n consistency), the route checks `settings.recording_consent_notice or
vocab.consent.notice_text` — operators who need custom legal text still set the env var; everyone
else gets the Ukrainian default, consistent with every other string on the page.

## Risks / Trade-offs

None significant — additive vocab key, backward-compatible settings default change (operators
who already set the env var see no change).
