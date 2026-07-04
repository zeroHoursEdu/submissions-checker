## Why

Quiz proctoring already reports camera/passive anti-cheat violations to the server and shows a text banner to the student on `warn`/`reduce_time`/`fail` actions, but there is no audible alert and teachers have no way to configure whether students are told they were caught. Some teachers running a strict policy want students to know immediately (visual + sound) that a violation was recorded, as a deterrent; others may want quieter enforcement. Today this is not configurable at all — the banner-on-non-flag behavior is hardcoded.

## What Changes

- Add a new `anti_cheat.notify_student` config key (boolean, default `true`) under `quiz.anti_cheat` in `config.yml`. When `true` (default, matches current behavior), violation banners continue to show for `warn`/`reduce_time`/`fail` actions.
- Add an audible alert (a short client-side sound, no new external asset dependency beyond what's bundled) that plays whenever a violation banner is shown to the student, gated by the same `notify_student` setting.
- `flag` actions remain silent to the student regardless of `notify_student` — flagging is inherently covert (teacher-only review), so this setting only affects `warn`/`reduce_time`/`fail` notifications, not flag visibility.
- When `notify_student` is `false`, no banner and no sound play for `warn`/`reduce_time`/`fail` — the violation is still recorded and still has its normal effect (time penalty applied, quiz still fails on `fail`), only the student-facing notification is suppressed.
- Update `docs/anti-cheat.md` and `plugins/e2e_test/config.yml` to document/demonstrate the new key.
- Configure `notify_student: true` (with sound) in the teacher subject repo `pythonBasicSubject`'s lab1 quiz, matching its existing strict policy (fail on phone/looking-away).

## Capabilities

### Modified Capabilities
- `quiz-proctoring`: adds the `anti_cheat.notify_student` config key controlling whether the student sees/hears a notification when a `warn`/`reduce_time`/`fail` violation is recorded (previously always shown, not configurable); `flag` actions remain unaffected (silent).

## Impact

- `src/submissions_checker/api/routes/student_quiz.py` (`report_violation`) — no change to rule matching/action logic; response already carries `action`/`message`, unaffected.
- `templates/student_quiz.html` (`report()`/`showBanner()`) — gate banner display + add sound playback based on a new `cfg.notify_student` flag (sourced from `anti_cheat.notify_student`), threaded into `proctoring_config`/`anti_cheat_config` already passed to the template.
- Quiz config schema (informal, in `subjects_assignment.py` docstring, `docs/anti-cheat.md`, `openspec/specs/quiz-proctoring/spec.md`) — document new key.
- `plugins/e2e_test/config.yml` — reference example updated.
- `/home/vampir/petProjects/pythonBasicSubject/config.yml` — lab1 quiz gets `notify_student: true`.
- No DB schema change (config lives in existing JSONB blobs), no new dependencies.
