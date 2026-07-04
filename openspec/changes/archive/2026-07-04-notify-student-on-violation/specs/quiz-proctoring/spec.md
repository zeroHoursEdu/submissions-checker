## ADDED Requirements

### Requirement: Configurable student violation notification
The system SHALL support an `anti_cheat.notify_student` boolean config key (default `true` when absent) that controls whether the student is shown a visible banner and played an audible alert when a `warn`, `reduce_time`, or `fail` anti-cheat action is applied, for both passive (`tab_switch`, `window_blur`, `resize`, `copy_attempt`, `right_click`, `keyboard_shortcut`, `fullscreen_exit`) and camera-sourced events. The setting SHALL have no effect on `flag` actions, which remain silent to the student regardless of its value. The setting SHALL NOT alter the underlying violation recording, time penalty, or fail outcome — only the student-facing notification.

#### Scenario: Default behavior unchanged
- **WHEN** a quiz's `anti_cheat` config has no `notify_student` key and a `warn` action fires
- **THEN** the student sees the existing warning banner and hears an audible alert, identical in spirit to current behavior

#### Scenario: Notifications explicitly enabled with sound
- **WHEN** `notify_student: true` and a `fail` action fires from either a passive or camera event
- **THEN** the student sees the failure banner and an audible alert plays before the quiz auto-submits

#### Scenario: Notifications disabled
- **WHEN** `notify_student: false` and a `reduce_time` action fires
- **THEN** the attempt's remaining time is still reduced by the configured penalty, but no banner is shown and no sound plays

#### Scenario: Flag actions stay silent regardless of setting
- **WHEN** `notify_student: true` and an event reaches a rule whose action is `flag`
- **THEN** no banner is shown and no sound plays to the student, identical to current flag behavior

#### Scenario: Audible alert accompanies the banner
- **WHEN** `notify_student` is enabled (default or explicit `true`) and a `warn`, `reduce_time`, or `fail` banner is shown
- **THEN** a short audible alert tone plays in the browser at the same time, generated client-side with no external audio asset
