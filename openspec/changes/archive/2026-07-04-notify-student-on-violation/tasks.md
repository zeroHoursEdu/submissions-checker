## 1. Client-side notification helper

- [x] 1.1 In `templates/student_quiz.html`'s passive anti-cheat block (~line 227-319), add a lazily-created shared `AudioContext` (module/script-scoped) and a `playAlertSound()` function that synthesizes a short (~150ms) sine-tone beep, wrapped in try/catch.
- [x] 1.2 In the same block, read `notifyStudent` from `ac.notify_student` (default `true` when key absent), and add a `notify(msg, level)` helper that calls `showBanner(msg, level)` + `playAlertSound()` only when `notifyStudent` is true.
- [x] 1.3 Replace the three `showBanner(...)` calls in this block's `report()` (`fail`, `reduce_time`, `warn` branches) with `notify(...)` calls. Leave `flag` untouched (no call site).

## 2. Camera block notification wiring

- [x] 2.1 In the camera proctoring `<script type="module">` block (~line 322-509), read `notifyStudent` from a new Jinja expression sourced from `anti_cheat_config.notify_student` (default `true`), independent of `cfg`/`proctoring_config` (which only holds the `camera` sub-object).
- [x] 2.2 Reuse the same beep mechanism as task 1.1 — either share via a `window`-scoped function set by the passive block, or duplicate the small synth function locally if module load order can't be guaranteed; document the choice with a one-line comment.
- [x] 2.3 Add the same `notify(msg, level)` gating helper and replace this block's three `showBanner(...)` calls (`fail`, `reduce_time`, `warn`) with `notify(...)`. Leave `flag` untouched.

## 3. Config schema + docs

- [x] 3.1 Update the `SubjectsAssignment` config docstring (`src/submissions_checker/db/models/subjects_assignment.py`) to mention `anti_cheat.notify_student` alongside the existing `anti_cheat`/`camera` documentation.
- [x] 3.2 Update `docs/anti-cheat.md` with the new `notify_student` key: purpose, default, and that it doesn't affect `flag`.
- [x] 3.3 Update `plugins/e2e_test/config.yml`'s quiz `anti_cheat` block to include `notify_student: true` as a documented example.

## 4. Tests

- [x] 4.1 Add/extend a functional test in `tests/functional/test_student_quiz_proctoring.py` (or `test_student_quiz.py`) asserting the rendered quiz page's inline config includes `notify_student` sourced correctly from `config_snapshot.anti_cheat` (default true when absent, explicit false when set).
- [x] 4.2 Add a unit/functional test confirming `report_violation`'s behavior (time penalty applied, `_force_fail` set, violations counted) is unchanged regardless of `notify_student` — i.e. this key only affects template-rendered client config, not `report_violation`'s response/DB effects.

## 5. Configure pythonBasicSubject

- [x] 5.1 Add `notify_student: true` to lab1's `quiz.anti_cheat` block in `/home/vampir/petProjects/pythonBasicSubject/config.yml`.
- [x] 5.2 Validate the file still parses (`python3 -c "import yaml; yaml.safe_load(open('config.yml'))"`).
