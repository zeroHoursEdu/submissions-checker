## Context

The app uses FastAPI with Jinja2 templates. Each route module instantiates its own `Jinja2Templates(directory="templates")` and calls `templates.TemplateResponse(...)` with a per-route context dict. All UI text is currently hardcoded English strings inside templates. There are ~30 HTML templates and 8 route modules.

PyYAML is already in `pyproject.toml`. No new runtime dependencies are needed.

## Goals / Non-Goals

**Goals:**
- Load vocabulary files from `i18n/` on startup; derive available languages from filenames
- Resolve active language per-request from a cookie, falling back to the first discovered language
- Inject `vocab` dict and `available_languages` list into every template context automatically — without touching every `TemplateResponse` call in 8 route files
- Language selector dropdown in `base.html`, driven by `available_languages`
- A `POST /set-language` endpoint that sets the cookie and redirects back
- All visible UI strings in templates replaced with `{{ vocab.<key> }}`
- Ukrainian vocabulary file (`i18n/uk.yml`) covering all current UI strings

**Non-Goals:**
- Pluralization / ICU message format
- Per-user persisted language preference in the database
- RTL layout support
- Translation of dynamic content from the database (subject names, assignment titles, etc.)
- Admin/teacher UI separation — same vocabulary applies to all roles

## Decisions

### D1 — Shared `Jinja2Templates` singleton with an injecting wrapper

**Decision**: Centralise all template rendering through a single `src/submissions_checker/core/templates.py` module that exports one `templates` object and a `render()` helper. The helper merges `vocab` and `available_languages` into every context before calling the underlying `TemplateResponse`.

**Why over alternatives**:
- *Jinja2 globals*: `env.globals` is static; it cannot vary per-request (language depends on the cookie).
- *Middleware injecting into `request.state`*: Requires every template to use `{{ request.state.vocab.x }}` — very verbose and requires touching every template key reference anyway.
- *Wrapper helper*: Routes call `render(request, "template.html", {...})` instead of `templates.TemplateResponse(...)`. That's a one-line change per call site; no context dict change. Adding `vocab` and `available_languages` happens in one place.

All route modules will import `render` from `core.templates` and replace their `TemplateResponse` calls.

### D2 — File-based language registry loaded at startup

**Decision**: On startup (`lifespan` in `main.py`), call `load_vocabularies(Path("i18n/"))`. This scans `*.yml` files, loads each, stores them in a module-level dict keyed by language code (filename stem, e.g. `"uk"`), and builds an `AVAILABLE_LANGUAGES` list of `{"code": "uk", "label": "Українська"}` dicts. The label is read from a special top-level key `_meta.label` in each YAML file.

**Why**: Simple, zero-config, zero-database. Adding a new language is dropping a YAML file. The startup scan keeps the list fresh without a deploy-time config change.

### D3 — Language stored in a cookie named `lang`

**Decision**: `POST /set-language` accepts a form field `lang`, validates it against `AVAILABLE_LANGUAGES`, sets a long-lived cookie (`max_age=365*24*3600`, `samesite=lax`), and redirects to `Referer` or `/`.

**Why over query-param or session**: Cookies persist across page navigations and don't pollute URLs. No session-storage complexity.

### D4 — Vocabulary key structure: flat dot-namespaced keys in YAML

**Decision**: The YAML file uses nested sections matching page/component groups (e.g. `nav`, `login`, `teacher_dashboard`). In templates, accessed as `{{ vocab.nav.sign_out }}`. The `load_vocabularies` function loads each YAML as-is (nested dict); no flattening.

**Why**: Nested YAML is readable for translators, and Jinja2 attribute access on dicts works naturally with dot notation via the `vocab` dict object (Jinja2 resolves `vocab.nav.sign_out` as `vocab["nav"]["sign_out"]`).

## Risks / Trade-offs

- [Coverage gaps] If a new template is added without adding its keys to `uk.yml`, Jinja2 will raise `UndefinedError` at render time → Mitigation: use Jinja2's `Undefined` set to `DebugUndefined` in development so missing keys render visibly as `{{ key }}` instead of crashing; production keeps strict mode.
- [Missing vocab for new languages] A future language YAML that omits some keys will cause render errors → Mitigation: `render()` falls back to the first registered language for any missing key (shallow merge).
- [All-route refactor] Replacing `TemplateResponse` calls across 8 files is mechanical but broad → Mitigation: it's a pure call-site rename with no logic change; easy to review and revert.
- [Cookie not sent on first load] First-time visitors get the default language (first YAML file discovered alphabetically) → Acceptable; Ukrainian is the only file so it is always the default.

## Migration Plan

1. Add `i18n/uk.yml` (all keys)
2. Add `src/submissions_checker/core/i18n.py` (loader + `get_vocab`)
3. Add `src/submissions_checker/core/templates.py` (shared `templates` singleton + `render` helper)
4. Register startup loader in `main.py` lifespan
5. Add `POST /set-language` route in a new `api/routes/i18n.py` and include it in `create_app`
6. Update all 8 route files to import `render` from `core.templates`, drop local `Jinja2Templates` instantiation, and replace `templates.TemplateResponse` calls with `render`
7. Replace hardcoded English strings in all `templates/*.html` with `{{ vocab.<section>.<key> }}`
8. Add language selector to `templates/base.html`

Rollback: revert changes to route files and templates; the `i18n/` directory and new core modules are additive and harmless if unused.
