## Context

`templates/student_quiz.html` has two independent client-side scripts that each report anti-cheat events to `POST /portal/quiz/{attempt_id}/event` and locally define their own `showBanner()`/`report()` pair:
- The **passive** block (~line 227-319, classic `<script>`) — `tab_switch`, `window_blur`, `resize`, `copy_attempt`, `right_click`, `keyboard_shortcut`, `fullscreen_exit`.
- The **camera** block (~line 322-509, `<script type="module">`) — `camera_face_absent`, `camera_multiple_faces`, `camera_looking_away`, `camera_phone_detected`, `camera_blocked`.

Both read the action returned by the server (`fail`/`reduce_time`/`warn`/`flag`) and only call `showBanner()` for `fail`/`reduce_time`/`warn` — `flag` already has no matching branch in either `report()`, so it is silent today with no extra code. Both blocks are independently rendered from the same server-side `anti_cheat_config` (`attempt.config_snapshot["anti_cheat"]`), so each can read a new key directly via Jinja without cross-script coupling.

## Goals / Non-Goals

**Goals:**
- Let a teacher configure, per quiz, whether the student sees/hears a notification when a `warn`/`reduce_time`/`fail` violation fires.
- Add a real audible alert, not just the existing text banner.
- Keep `flag` silent unconditionally (covert by design) — `notify_student` must not leak flags to the student.
- Zero new asset/dependency footprint.

**Non-Goals:**
- No granular per-action-type notify toggle (e.g. notify-on-warn-but-not-fail) — one boolean covers all three visible actions, matching what was asked for.
- No change to the rule engine, event counting, or server-side `report_violation` logic — this is purely about what the client shows/plays.
- No custom/uploadable sound files or volume config.

## Decisions

**1. Sound via generated WebAudio tone, not a bundled audio file.**
A short oscillator beep (`AudioContext` + `OscillatorNode`, ~150ms sine tone) is synthesized in JS instead of shipping an `.mp3`/`.wav`. Alternative considered: bundle a static sound asset served via the app's static files. Rejected — adds a binary asset + a static-serving path for something a 10-line WebAudio snippet does with no new file, no CDN, no offline-availability risk (matches the existing "no video frames leave the device" minimal-footprint style of this feature).

**2. Reuse one shared `AudioContext`, created lazily on first alert.**
Creating a new `AudioContext` per violation would leak contexts under repeated violations. A single module-scope (or `window`-scoped, since two independent scripts both need it) context is created on first use and reused.

**3. `notify_student` is read independently in both script blocks from server-rendered `anti_cheat_config.notify_student` (default `true`).**
Rather than introducing shared JS state between the classic script and the ES module (which load/execute as two separate top-level scripts), each block's existing Jinja-rendered config object (`ac` in the passive block, and a new small Jinja expression in the camera block) carries the same flag independently. This avoids restructuring the two blocks into one shared script, which is out of scope here.

**4. Wrap `showBanner()` + beep behind a new `notify(msg, level)` helper in each block**, used only at the three existing `fail`/`reduce_time`/`warn` call sites. `flag` keeps having no call site, so it stays silent with no extra guard needed — satisfies the "flag unaffected" requirement by construction rather than a runtime check.

**5. Config key placement: `anti_cheat.notify_student` (sibling of `camera`/`rules`), not under `camera`.**
It governs both passive and camera-sourced violations, so it belongs at the `anti_cheat` level the same as `rules`, not nested under `camera` (which the passive block doesn't read).

## Risks / Trade-offs

- **Browser autoplay/audio-gesture restrictions** may block the beep on some browsers until a user gesture has occurred on the page → mitigated by wrapping playback in try/catch (already the pattern used throughout this file); banner still shows visually regardless of audio failure.
- **Existing duplication between the two script blocks** (each has its own `showBanner`/`report`) is not being refactored away here — the new `notify()` helper is added twice (once per block) to match the existing structure rather than a larger unrelated refactor.
