# language-selector-ui Specification

## Purpose
Records that the UI ships a single language and that the in-header language selector was
removed. Kept so nobody re-adds a selector without first shipping a second vocabulary.

## Requirements
### Requirement: The UI ships one language and renders no language selector
The system SHALL ship exactly one vocabulary (`i18n/uk.yml`) and SHALL NOT render a language
selector or expose a `/set-language` endpoint. Only `en.yml` was never committed; the selector,
the language registry and the cookie route were removed on 2026-09-17 (see
`docs/feature_audit.md` B4). Reintroducing a selector SHALL be a new change that ships the
second vocabulary in the same change.

#### Scenario: No selector in the header
- **WHEN** any page extending `base.html` is rendered
- **THEN** no `<select name="lang">` element and no `/set-language` form are present

#### Scenario: Language route is gone
- **WHEN** a client POSTs to `/set-language`
- **THEN** the response is 404
