## ADDED Requirements

### Requirement: Language selector dropdown is rendered in the header when multiple languages are available
The system SHALL render a language selector `<select>` element in `base.html`'s header navigation area. The dropdown SHALL be populated with the list of available languages supplied in the `available_languages` template context variable. The currently active language SHALL be shown as the selected option. The selector SHALL only be visible when `available_languages` contains more than one entry.

#### Scenario: Only one language registered
- **WHEN** `available_languages` contains a single entry
- **THEN** no language selector element is rendered in the header

#### Scenario: Multiple languages registered
- **WHEN** `available_languages` contains two or more entries
- **THEN** a `<select>` dropdown is rendered in the header showing all available languages, with the current language pre-selected

### Requirement: Selecting a language sets the lang cookie and redirects back
The system SHALL expose a `POST /set-language` endpoint. When a language code is submitted, the system SHALL validate it against the registered languages, set the `lang` cookie (path `/`, `max_age` one year, `samesite=lax`, `httponly=true`), and redirect the client to the value of the `Referer` header or `/` if absent.

#### Scenario: Valid language selected
- **WHEN** a POST request is sent to `/set-language` with form field `lang=uk`
- **THEN** the response sets `lang=uk` cookie and redirects (302) to the referring page

#### Scenario: Invalid language code submitted
- **WHEN** a POST request is sent to `/set-language` with form field `lang=xx` where `xx` is not registered
- **THEN** the system returns a 400 response without setting the cookie

#### Scenario: No Referer header
- **WHEN** a POST request to `/set-language` has no `Referer` header
- **THEN** the redirect target is `/`

### Requirement: All visible UI strings in templates use vocabulary keys
Every hardcoded user-visible string in `templates/*.html` SHALL be replaced with a Jinja2 expression referencing the `vocab` dict (e.g. `{{ vocab.nav.sign_out }}`). Dynamic content fetched from the database (subject names, assignment titles, student names) is explicitly excluded.

#### Scenario: Template renders with active vocabulary
- **WHEN** a page is rendered with `lang=uk`
- **THEN** all static UI labels (navigation items, button labels, headings, form labels, status messages) are displayed in Ukrainian

#### Scenario: Template missing a vocabulary key in development
- **WHEN** a template references a key absent from the active vocabulary YAML and the environment is `development`
- **THEN** the missing reference renders as a visible placeholder (e.g. `{{ vocab.missing_key }}`) rather than crashing
