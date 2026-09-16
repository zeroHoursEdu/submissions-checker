## Why

All UI text in the application is currently hardcoded in English inside Jinja2 templates, making it inaccessible to Ukrainian-speaking students and teachers. The system needs to display everything in Ukrainian now, with infrastructure ready to add more languages in the future without further structural changes.

## What Changes

- Add an `i18n/` directory at the project root to hold per-language YAML files
- Add `uk.yml` (Ukrainian) as the first vocabulary file containing all UI text labels used across templates
- Implement a vocabulary loader that scans the `i18n/` folder at startup and registers available languages
- Expose available languages on app state so templates can render a language selector dropdown
- Update `base.html` to include a language dropdown in the header, populated from discovered languages
- Store the user's selected language in a cookie and pass the active vocabulary dict to every template render
- Update all Jinja2 templates to use vocabulary keys (`{{ vocab.label_key }}`) instead of hardcoded English strings

## Capabilities

### New Capabilities
- `i18n-vocabulary-loader`: Scans `vocabularies/*.yml` at startup, builds a registry of available languages, and makes the active vocabulary accessible during request rendering
- `language-selector-ui`: Language dropdown in the app header driven by discovered languages; selection stored in a cookie and applied per-request

### Modified Capabilities
- (none — this is purely additive at the spec level; existing templates are updated during implementation but their behavioral requirements do not change)

## Impact

- **Templates**: All `templates/*.html` files need labels replaced with `{{ vocab.<key> }}` references
- **`src/submissions_checker/main.py`**: Startup must invoke the vocabulary loader and attach available-language list to app state
- **`src/submissions_checker/core/`**: New `i18n.py` module for loader + per-request vocabulary resolution
- **New file**: `vocabularies/uk.yml` with Ukrainian translations of every UI string
- **`templates/base.html`**: Language dropdown added to the header nav
- **Routing layer**: A `/set-language` endpoint (or cookie-set middleware) to persist the user's language choice
- No database migrations, no breaking API changes, no new dependencies (PyYAML is already available via FastAPI ecosystem or can be added)
