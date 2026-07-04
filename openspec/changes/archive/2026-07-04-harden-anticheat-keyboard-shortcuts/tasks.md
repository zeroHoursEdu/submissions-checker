## 1. Client-side detection

- [x] 1.1 In `templates/student_quiz.html`'s `keyboard_shortcut` keydown listener (~line 327-332), replace the `e.ctrlKey`-only check with `const mod = e.ctrlKey || e.metaKey;` and use `mod` in place of `e.ctrlKey` for the existing `casp` combo check.
- [x] 1.2 In the same handler, extend the `sus` predicate to also match view-source (`mod && key === 'u'`) and devtools-panel combos (`mod && e.shiftKey && 'ijc'.includes(key)` for Ctrl/Cmd+Shift+I/J/C, and `mod && e.altKey && 'ijc'.includes(key)` for Cmd+Option+I/J/C), where `key` is the existing lowercased `e.key`. Leave the `F12`/`PrintScreen` checks unchanged.

## 2. Docs

- [x] 2.1 Update the `keyboard_shortcut` row in `docs/anti-cheat.md`'s detection table to list the specific combos covered (Ctrl/Cmd+C/A/S/P, Ctrl/Cmd+U, Ctrl+Shift+I/J/C, Cmd+Option+I/J/C, F12, PrintScreen).
- [x] 2.2 Add a line to `docs/anti-cheat.md`'s Limitations section stating that devtools/view-source opened via the browser's menu (not a keyboard shortcut) cannot be detected — no DOM event fires for menu-driven actions.

## 3. Tests

- [x] 3.1 Add/extend a functional or JS-level test asserting a simulated `keydown` with `metaKey: true, key: 'c'` (Mac Cmd+C) triggers the same `keyboard_shortcut` report path as `ctrlKey: true, key: 'c'` does today.
- [x] 3.2 Add a test asserting `keydown` with `ctrlKey: true, key: 'u'` and with `ctrlKey: true, shiftKey: true, key: 'i'` both trigger `keyboard_shortcut` detection.
- [x] 3.3 Confirm existing `keyboard_shortcut` tests (Ctrl+C/A/S/P, F12, PrintScreen) still pass unchanged.
