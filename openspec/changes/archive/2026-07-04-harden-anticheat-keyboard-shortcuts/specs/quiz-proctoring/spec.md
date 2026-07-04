## ADDED Requirements

### Requirement: Cross-platform and devtools-aware keyboard-shortcut detection

When a quiz's `anti_cheat.rules[]` includes a `keyboard_shortcut` rule, the system SHALL report a
`keyboard_shortcut` event on any of the following, regardless of whether the platform modifier is
Ctrl (Windows/Linux) or Cmd (macOS): copy/select-all/save/print combos (Ctrl/Cmd+C/A/S/P),
view-source (Ctrl/Cmd+U), devtools-panel combos (Ctrl+Shift+I/J/C on Windows/Linux, Cmd+Option+I/
J/C on macOS), `F12`, and `PrintScreen`. Detecting any of these SHALL prevent the browser's
default action for that key combination and report exactly one `keyboard_shortcut` event, matched
against `anti_cheat.rules[]` identically to how the event is handled today — no new event type,
threshold, or action is introduced.

#### Scenario: Mac copy shortcut is detected

- **WHEN** a student on macOS presses Cmd+C during a quiz with a `keyboard_shortcut` rule
  configured
- **THEN** the browser's default copy action is prevented and a `keyboard_shortcut` event is
  reported, identical to what Ctrl+C already triggers on Windows/Linux

#### Scenario: View-source shortcut is detected

- **WHEN** a student presses Ctrl+U (or Cmd+U on macOS) during a quiz with a `keyboard_shortcut`
  rule configured
- **THEN** the default view-source action is prevented and a `keyboard_shortcut` event is reported

#### Scenario: Devtools panel shortcut is detected

- **WHEN** a student presses Ctrl+Shift+I (or Cmd+Option+I on macOS) during a quiz with a
  `keyboard_shortcut` rule configured
- **THEN** the default devtools-opening action is prevented and a `keyboard_shortcut` event is
  reported

#### Scenario: Menu-opened devtools remains undetected

- **WHEN** a student opens developer tools via the browser's menu (no keyboard shortcut involved)
- **THEN** no `keyboard_shortcut` event is reported — this remains a documented, accepted
  limitation since no DOM event fires for menu-driven browser actions
