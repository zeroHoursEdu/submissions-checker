## MODIFIED Requirements

### Requirement: Configurable notice text
The recording notice text SHALL be configurable (settings or template) so wording can be adjusted for jurisdiction, and SHALL cover webcam recording, snapshot storage, the purpose (exam integrity), and retention. When no operator override is configured, the notice text SHALL default to the localized text in the active i18n vocabulary, not a hardcoded English string, so it matches the language of the rest of the page.

#### Scenario: Notice content
- **WHEN** the consent screen is shown
- **THEN** it displays the configured notice text covering recording, storage, purpose, and retention

#### Scenario: Default notice matches page language
- **WHEN** no jurisdiction-specific override is configured
- **THEN** the notice text is read from the i18n vocabulary in the same language as the rest of the consent page, not a hardcoded English default

#### Scenario: Operator override still works
- **WHEN** an operator sets a jurisdiction-specific notice via configuration
- **THEN** that configured text is shown instead of the vocabulary default
