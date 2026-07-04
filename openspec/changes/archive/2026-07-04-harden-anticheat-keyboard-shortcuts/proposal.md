## Why

The passive anti-cheat `keyboard_shortcut` detector in `templates/student_quiz.html` only checks
`e.ctrlKey`, so any Mac student pressing Cmd+C/A/S/P — the platform-native modifier for copy,
select-all, save, print — bypasses detection entirely; the rule only ever fires for Windows/Linux
students using Ctrl. Separately, the detector doesn't recognize the two most common ways a student
opens page source or developer tools (Ctrl/Cmd+U for view-source, Ctrl+Shift+I/J/C or
Cmd+Option+I/J/C for devtools panels), even though F12 is already covered. This is a coverage gap
in existing anti-cheat detection, not a new capability — true prevention of devtools/view-source
opened via the browser's own menu (no keyboard shortcut at all) remains impossible from page JS,
consistent with the Limitations already documented in `docs/anti-cheat.md`.

## What Changes

- Fix the `keyboard_shortcut` keydown handler to check both `e.ctrlKey` and `e.metaKey`, so Mac
  Cmd-based shortcuts are detected on equal footing with Windows/Linux Ctrl-based ones.
- Extend the suspicious-shortcut match list to also cover Ctrl/Cmd+U (view source) and the
  devtools-panel combos Ctrl+Shift+I/J/C and Cmd+Option+I/J/C (Cmd+Alt+I/J/C).
- Update `docs/anti-cheat.md`'s `keyboard_shortcut` row and Limitations section to name the
  specific shortcuts now covered and to state plainly that devtools/view-source opened through the
  browser's menu (not a keyboard shortcut) still cannot be detected — no behavior claim changes,
  only the documented shortcut coverage.

## Capabilities

### Modified Capabilities
- `quiz-proctoring`: the passive `keyboard_shortcut` anti-cheat event must detect suspicious key
  combinations on both Ctrl-based (Windows/Linux) and Cmd-based (Mac) modifier keys, and must
  additionally recognize view-source and devtools-panel shortcuts, not just copy/select-all/
  save/print and F12/PrintScreen.

## Impact

- `templates/student_quiz.html` — the passive anti-cheat `keydown` listener's suspicious-shortcut
  predicate.
- `docs/anti-cheat.md` — `keyboard_shortcut` row and Limitations section wording.
- No config schema, DB, or API changes. No change to how `keyboard_shortcut` events are counted,
  matched against rules, or acted upon — only which key combinations are recognized as the event.
