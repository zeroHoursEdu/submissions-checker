# Spec: Recording Consent

## Purpose

Defines one-time student consent to webcam recording during proctored quizzes:
how consent is recorded on the account, the consent gate that blocks quiz
access until consent is given, and the configurable legal notice text.

## Requirements

### Requirement: One-time recording consent
The system SHALL record a student's consent to being recorded during proctored quizzes by stamping a `recording_consent_at` timestamp on the student account. The student account SHALL default to no consent (null) until the student explicitly agrees.

#### Scenario: New account has no consent
- **WHEN** a student account is created
- **THEN** `recording_consent_at` is null

#### Scenario: Agreeing stamps consent
- **WHEN** a student accepts the recording notice
- **THEN** `recording_consent_at` is set to the current time and persists across sessions

### Requirement: Consent gate before quiz access
The system SHALL present a one-time consent screen with the recording legal notice on first login and SHALL block access to quizzes until consent is recorded. Once consent is recorded, the screen SHALL NOT be shown again.

#### Scenario: First login shows consent screen
- **WHEN** a student with null `recording_consent_at` logs in
- **THEN** the consent notice is shown and quiz access is blocked until they agree

#### Scenario: Consent persists on later logins
- **WHEN** a student who has already consented logs in again
- **THEN** the consent screen is not shown and quizzes are accessible

#### Scenario: Quiz access blocked without consent
- **WHEN** a student with null `recording_consent_at` attempts to open a quiz directly
- **THEN** access is denied and the student is directed to the consent screen

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
