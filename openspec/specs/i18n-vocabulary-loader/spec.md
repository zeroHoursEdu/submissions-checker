# i18n-vocabulary-loader Specification

## Purpose
Loads the UI vocabulary from `i18n/*.yml` and injects it into every rendered template as `vocab`.
## Requirements
### Requirement: Vocabulary files are discovered from a dedicated directory
The system SHALL scan the `i18n/` directory at application startup and register every `*.yml` file found as a vocabulary. The language code SHALL be the filename stem (e.g. `uk.yml` → code `"uk"`). The first file in sorted order SHALL be the default language.

#### Scenario: Single vocabulary file present
- **WHEN** the application starts and `vocabularies/uk.yml` exists
- **THEN** the system registers exactly one vocabulary with code `"uk"`, which is the default

#### Scenario: Multiple vocabulary files present
- **WHEN** the application starts and `vocabularies/` contains `uk.yml` and `en.yml`
- **THEN** the system registers two vocabularies, one per file; the alphabetically first is the default

#### Scenario: No vocabulary files present
- **WHEN** the application starts and `vocabularies/` is empty or does not exist
- **THEN** the system continues startup with an empty registry and `vocab` renders as an empty dict

### Requirement: Active vocabulary is resolved per request from a cookie
The system SHALL read the `lang` cookie on every HTML request. If the cookie value matches a registered language code, that language's vocabulary SHALL be used. If the cookie is absent or invalid, the system SHALL fall back to the first registered language.

#### Scenario: Valid lang cookie
- **WHEN** a request arrives with cookie `lang=uk`
- **THEN** the Ukrainian vocabulary dict is injected into the template context as `vocab`

#### Scenario: Missing lang cookie
- **WHEN** a request arrives with no `lang` cookie
- **THEN** the vocabulary of the first registered language is used as the default

#### Scenario: Unknown lang cookie value
- **WHEN** a request arrives with `lang=xx` and `"xx"` is not a registered language
- **THEN** the system falls back to the first registered language without raising an error

### Requirement: Vocabulary dict is injected into every template context automatically
The system SHALL expose a `render()` helper function that wraps `Jinja2Templates.TemplateResponse`. This helper SHALL merge `vocab` (active language dict) into the template context before rendering, so individual route handlers do not need to supply it explicitly.

#### Scenario: Route calls render helper
- **WHEN** a route handler calls `render(request, "some.html", {"current_user": user})`
- **THEN** the rendered template receives `current_user` and `vocab` in its context

#### Scenario: Route-supplied key conflicts with injected key
- **WHEN** a route handler passes `{"vocab": custom_vocab}` explicitly
- **THEN** the route-supplied value takes precedence (render helper does not overwrite)

