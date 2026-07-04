## Why

Today a subject can enter the system two ways: (1) a teacher uploads a ZIP via `POST
/teacher/subjects/apply-config` (the API/UI path, owned by `ConfigApplyService`), or (2) a
maintainer symlinks or clones a subject repo into the `plugins/` directory, and the `PluginLoader`
service scans that directory on every app startup, silently upserting `Subject`/
`SubjectsAssignment` rows with **no `owner_id`** — a state that then permanently 403s every
ownership-gated teacher route for that subject (`docs/known_bugs.md` #1). This dual-path design
is confusing, undocumented to teachers, and the startup-scan path produces subjects nobody can
manage through the UI. The user wants a single, API/UI-only way to add subjects.

The blocker to simply deleting the startup scan: the actual checker code (`check.py`,
`validate.py`, fixtures, `checklib` references) that a `Subject` depends on at check-execution
time is read from `plugins_dir/<subjectCode>/` on every check run (`check_tasks.py`), regardless
of which path created the `Subject` row. `ConfigApplyService` today only persists `config.yml`
plus referenced images/content files to the DB and S3 — it never writes the actual checker code
anywhere on disk. Removing the startup scan without also fixing this would leave every
API/UI-created subject with a `Subject` row but no checkable code, failing every submission at
runtime with a missing `/plugin` mount.

## What Changes

- Remove the startup plugin-loader scan entirely: delete the `PluginLoader().load_all(...)` call
  from `main.py`'s `lifespan()`, and delete the now-dead `PluginLoader` service and its dedicated
  tests. **BREAKING**: subjects placed under `plugins/` via symlink/clone are no longer
  auto-registered on startup — there is no scan at all anymore.
- Extend `ConfigApplyService.apply()` to extract the **entire** uploaded ZIP tree (not just
  `config.yml` and referenced images) to `plugins_dir/<subjectCode>/` on disk, atomically
  replacing any previous version for that subject, so the exact same on-disk layout the old
  symlink workflow produced now comes from the upload itself — no manual placement step, ever.
  This makes the API/UI path the sole way a subject's checker code reaches disk, and
  `check_tasks.py`'s existing `plugin_dir` resolution keeps working unmodified.
- Update `docs/PLUGIN_AUTHORING.md` to describe zipping the whole plugin directory and uploading
  it through `POST /teacher/subjects/apply-config`, instead of symlinking into `plugins/`.
- Fix a real bug this change exposed in `tests/e2e/steps/subject_steps.py`'s `ensure_subject_exists`:
  it already registered its test subject via a real ZIP upload (the e2e stack's startup scan was
  already a no-op there, unrelated to this change), but assumed a fresh, unauthenticated page.
  Removing the startup scan makes that upload branch the guaranteed-first-invocation path, which
  exposed the assumption breaking whenever a Background logs in as a different role first
  (`analytics.feature` logs in as admin). Fixed with an explicit logout before the upload's login.
- Remove the existing dev-only symlinks under `plugins/` (`cppBasics -> ../../cppBasicSubject`,
  `pythonBasics -> ../../pythonBasicSubject`) — with the scan gone they no longer do anything at
  startup, and leaving them in place would misleadingly suggest the symlink workflow still works.
  Local dev switches to zipping `cppBasicSubject`/`pythonBasicSubject` and uploading through the
  teacher UI, same as any other subject.

## Capabilities

### Modified Capabilities
- `subject-config-apply`: the ZIP upload path SHALL extract the full plugin tree (checker
  scripts, fixtures, `checklib` references — not just `config.yml` and images) to
  `plugins_dir/<subjectCode>/` on disk as part of a successful apply, atomically replacing any
  prior version, so the API/UI path is sufficient on its own to make a subject checkable.
- `assignment-checking`: the "Subject-per-repo packaging with custom image" requirement's
  loading mechanism changes from "placed under `plugins/` via symlink or clone, engine scans at
  startup" to "uploaded as a ZIP via the config-apply endpoint, which extracts it to `plugins_dir`
  on the API/UI-driven owner's behalf" — no startup scan exists anymore.

## Impact

- `src/submissions_checker/main.py` — remove step 3 (`PluginLoader` call) from `lifespan()`.
- `src/submissions_checker/services/plugin_loader.py` — deleted (dead code, no callers left).
- `tests/integration/test_plugin_loader.py` — deleted (tests deleted code).
- `tests/unit/test_main_lifespan.py` — updated to no longer expect a `PluginLoader` call.
- `src/submissions_checker/services/config_apply.py` — `ConfigApplyService` gains a
  `plugins_dir: Path` dependency and a new execution step that extracts the uploaded ZIP's full
  tree to `plugins_dir/<subjectCode>/`.
- `src/submissions_checker/api/routes/teacher_portal.py` — `ConfigApplyService(storage)`
  construction site passes the new `plugins_dir` argument.
- `docs/PLUGIN_AUTHORING.md` — rewritten around "build the ZIP, upload it" instead of
  "symlink into `plugins/`".
- `tests/e2e/steps/subject_steps.py` — `ensure_subject_exists` gets an explicit logout before its
  login/upload, fixing a latent bug newly guaranteed to trigger by this change (see above).
- No DB schema change: `Subject`/`SubjectsAssignment`/`SubjectPluginConfig` are unaffected;
  `owner_id` is already always set by `ConfigApplyService`.
- `plugins/cppBasics`, `plugins/pythonBasics` — dev-only symlinks deleted; the `e2e_test`
  directory (a real extracted-in-place fixture, not a symlink) is untouched since it's reseeded
  via upload per the e2e task above.
- Out of scope: backfilling `owner_id` for subjects previously created by the startup scan
  (`docs/known_bugs.md` #1). The `cppBasics`/`pythonBasics` subject DB rows (if already present
  from a prior scan) still have `owner_id = NULL` after this change; deleting their symlinks does
  not touch those DB rows. Re-uploading `cppBasicSubject`/`pythonBasicSubject` via the API once
  will fix their ownership going forward, using the mechanism this change builds.
