## Context

`templates/student_quiz.html`'s passive anti-cheat block wires a single `keydown` listener when a
`keyboard_shortcut` rule is configured:

```js
if (hasRule('keyboard_shortcut')) {
  document.addEventListener('keydown', e => {
    const sus = (e.ctrlKey && 'casp'.includes((e.key || '').toLowerCase())) ||
                e.key === 'F12' || e.key === 'PrintScreen';
    if (sus) { e.preventDefault(); report('keyboard_shortcut'); }
  });
}
```

Only `e.ctrlKey` is checked, so on macOS — where copy/select-all/save/print use Cmd, not Ctrl —
none of the `casp` combos are ever detected. View-source (`Ctrl+U` / `Cmd+U`) and the devtools
panel shortcuts (`Ctrl+Shift+I/J/C`, `Cmd+Option+I/J/C`) aren't in the list at all, even though
F12 already is.

## Goals / Non-Goals

**Goals:**
- Detect the same set of "suspicious" actions regardless of whether the student is on Windows/
  Linux (Ctrl) or macOS (Cmd).
- Recognize the keyboard paths to view-source and devtools, matching the level of coverage F12
  already gets.

**Non-Goals:**
- Detecting devtools/view-source opened via the browser's menu bar or right-click-less touch
  gesture — there is no DOM event for that; already an accepted, documented limitation.
- Changing the `keyboard_shortcut` rule engine, thresholds, or actions — only which physical key
  combinations count as the event.
- Adding new event types — `keyboard_shortcut` remains the single reported event name for all of
  these combos, same as today's `casp`/F12/PrintScreen.

## Decisions

**Check `e.ctrlKey || e.metaKey` instead of `e.ctrlKey` alone.** `metaKey` is the Cmd key on
macOS and the Windows key on Windows/Linux (rarely used in browser shortcuts), so OR-ing it in
adds Mac coverage without introducing false positives on other platforms — the Windows key alone
practically never fires app-level `keydown` combos browsers act on.

**Add a separate devtools/view-source check rather than folding into `casp`.** The existing `casp`
string check matches a single lowercase letter key with a modifier. Devtools shortcuts need
`Shift` or `Alt/Option` as a second modifier (e.g. `Ctrl+Shift+I`), so they're expressed as their
own boolean clause: `(mod && e.key === 'u') || (mod && e.shiftKey && 'ijc'.includes(key)) ||
(e.altKey && mod && 'ijc'.includes(key))` where `mod = e.ctrlKey || e.metaKey`. Kept as one
`sus` boolean, still one `report('keyboard_shortcut')` call — no new event type, no config
change.

**Leave `F12`/`PrintScreen` checks untouched.** Already platform-agnostic (no modifier key
involved), no change needed.

## Risks / Trade-offs

- [Risk] `e.metaKey` on Windows is the Windows/Super key; some legitimate window-manager shortcuts
  use Win+key. → Mitigation: this only matters when *also* combined with `casp`/`u`/`ijc` keys,
  which Win+key combos don't normally use in-browser; same false-positive shape the existing
  Ctrl-only check already accepts for those keys.
- [Risk] Still fully bypassable via the browser's View menu or a bookmarklet with no keyboard
  involved at all. → Mitigation: already an explicit, honest limitation in `docs/anti-cheat.md`;
  this change doesn't claim otherwise, it only closes the keyboard-path gap.
