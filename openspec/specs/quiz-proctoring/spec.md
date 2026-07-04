# Spec: Quiz Proctoring

## Purpose

Defines per-quiz webcam proctoring: configuration, client-side ML detection of
anti-cheat conditions, integration with the existing punishment engine, the
camera-required start gate, snapshot evidence capture, teacher review of
evidence, and the in-quiz recording notice.

## Requirements

### Requirement: Configurable webcam proctoring for quizzes
The system SHALL support per-quiz webcam proctoring, configured under `quiz.anti_cheat.camera` in the subject config and snapshotted into the quiz attempt's `config_snapshot` at attempt start. When `camera.enabled` is false or absent, the quiz SHALL behave exactly as today with no camera activity.

#### Scenario: Proctoring disabled
- **WHEN** a quiz whose config has no `camera` block or `camera.enabled: false` is opened
- **THEN** no camera is requested and the quiz runs with only the existing passive anti-cheat

#### Scenario: Proctoring enabled
- **WHEN** a quiz whose config has `camera.enabled: true` is opened
- **THEN** the page requests webcam access and begins client-side detection before the quiz form is interactive

#### Scenario: Config isolation across attempts
- **WHEN** the camera config is changed after an attempt has started
- **THEN** the in-progress attempt continues to use the camera config captured in its `config_snapshot`

### Requirement: Client-side detection emits anti-cheat events
The system SHALL run ML detection in the student's browser (no frames sent for detection) and report detected conditions as anti-cheat events to the existing `POST /portal/quiz/{attempt_id}/event` endpoint, using the event names `camera_face_absent`, `camera_multiple_faces`, `camera_looking_away`, `camera_phone_detected`, and `camera_blocked`. Each event SHALL be reported only after the condition is sustained for the configured `sustain_seconds` (debounced), so that noisy per-frame detections become discrete violation events.

#### Scenario: No face sustained
- **WHEN** no face is visible for at least `face_absent.sustain_seconds`
- **THEN** the client reports a single `camera_face_absent` event

#### Scenario: Second person appears
- **WHEN** more than one face is visible for at least `multiple_faces.sustain_seconds`
- **THEN** the client reports a `camera_multiple_faces` event

#### Scenario: Looking away
- **WHEN** head yaw or pitch exceeds the configured thresholds for at least `looking_away.sustain_seconds`
- **THEN** the client reports a `camera_looking_away` event

#### Scenario: Phone in frame
- **WHEN** the object detector identifies a phone or book above `phone.min_confidence`
- **THEN** the client reports a `camera_phone_detected` event

#### Scenario: Transient detection does not fire
- **WHEN** a condition appears but clears before its `sustain_seconds` elapses
- **THEN** no event is reported for that condition

### Requirement: Camera events use the existing punishment engine
The system SHALL evaluate camera events through the existing anti-cheat rule engine without modification: each event is counted per attempt and matched against `anti_cheat.rules[]` by event name, applying the configured action (`fail`, `reduce_time`, `warn`, or `flag`) when the rule threshold is reached. Camera events without a matching rule SHALL be counted but take no action.

#### Scenario: Strict phone rule fails the quiz
- **WHEN** `camera_phone_detected` reaches the threshold of a rule with action `fail`
- **THEN** the attempt is marked failed (`_force_fail`) and finalized as `VIOLATION_FAIL`, identical to existing fail behavior

#### Scenario: Repeated look-aways warn then penalize
- **WHEN** `camera_looking_away` reaches a `warn` rule threshold
- **THEN** the student sees a warning banner and the attempt continues, identical to existing warn behavior

#### Scenario: Camera event without a rule
- **WHEN** a camera event is reported but no rule references that event
- **THEN** the event is counted in `violations` and no action is taken

### Requirement: Camera-required start gate
The system SHALL block the start of a proctored quiz when `camera.require_camera` is true and the webcam is unavailable or permission is denied. The action SHALL follow `camera.on_no_camera` (`fail` or `warn`), and a `camera_blocked` event SHALL be reported.

#### Scenario: Camera denied with require_camera and on_no_camera fail
- **WHEN** the student denies webcam access on a quiz with `require_camera: true` and `on_no_camera: fail`
- **THEN** the quiz form is not made available, a clear message is shown, and a `camera_blocked` event is reported

#### Scenario: Camera optional
- **WHEN** `require_camera` is false and no camera is available
- **THEN** the quiz proceeds without blocking

### Requirement: Snapshot evidence capture
When `camera.capture_snapshots` is true, the system SHALL capture a webcam frame and store it to object storage on events whose resulting action is listed in `camera.snapshot_on`. Each stored frame SHALL be recorded as a `quiz_attempt_snapshots` row referencing the attempt, the event type, the storage key, the URL, and the capture time. The snapshot upload endpoint SHALL enforce the same ownership and in-progress checks as the event endpoint and reject oversized or non-image uploads.

#### Scenario: Flagged event captures a frame
- **WHEN** a camera event results in an action listed in `snapshot_on` and `capture_snapshots` is true
- **THEN** the client uploads the current frame and a `quiz_attempt_snapshots` row is created with the S3 key and URL

#### Scenario: Capture disabled
- **WHEN** `capture_snapshots` is false
- **THEN** no frame is uploaded for any event

#### Scenario: Snapshot upload rejects non-owner
- **WHEN** a snapshot upload is attempted for an attempt the requester does not own
- **THEN** the endpoint responds 403 and stores nothing

#### Scenario: Snapshot upload rejects after attempt ends
- **WHEN** a snapshot upload is attempted for an attempt that is not `IN_PROGRESS`
- **THEN** the endpoint rejects the upload and stores nothing

### Requirement: Teacher review of proctoring evidence
The teacher assignment review SHALL surface, per flagged attempt, the camera violation counts and thumbnails of any captured snapshots, so teachers can judge borderline cases.

#### Scenario: Teacher views flagged attempt
- **WHEN** a teacher opens an assignment with a quiz attempt that has camera violations and snapshots
- **THEN** the page shows the violation breakdown and the snapshot thumbnails for that attempt

### Requirement: Recording notice during the quiz
A proctored quiz page SHALL display a visible "you are being recorded" notice while the camera is active.

#### Scenario: Notice shown on proctored quiz
- **WHEN** a proctored quiz with the camera active is displayed
- **THEN** a recording notice is visible to the student

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

### Requirement: Cross-platform and devtools-aware keyboard-shortcut detection
When a quiz's `anti_cheat.rules[]` includes a `keyboard_shortcut` rule, the system SHALL report a `keyboard_shortcut` event on any of the following, regardless of whether the platform modifier is Ctrl (Windows/Linux) or Cmd (macOS): copy/select-all/save/print combos (Ctrl/Cmd+C/A/S/P), view-source (Ctrl/Cmd+U), devtools-panel combos (Ctrl+Shift+I/J/C on Windows/Linux, Cmd+Option+I/J/C on macOS), `F12`, and `PrintScreen`. Detecting any of these SHALL prevent the browser's default action for that key combination and report exactly one `keyboard_shortcut` event, matched against `anti_cheat.rules[]` identically to how the event is handled today — no new event type, threshold, or action is introduced.

#### Scenario: Mac copy shortcut is detected
- **WHEN** a student on macOS presses Cmd+C during a quiz with a `keyboard_shortcut` rule configured
- **THEN** the browser's default copy action is prevented and a `keyboard_shortcut` event is reported, identical to what Ctrl+C already triggers on Windows/Linux

#### Scenario: View-source shortcut is detected
- **WHEN** a student presses Ctrl+U (or Cmd+U on macOS) during a quiz with a `keyboard_shortcut` rule configured
- **THEN** the default view-source action is prevented and a `keyboard_shortcut` event is reported

#### Scenario: Devtools panel shortcut is detected
- **WHEN** a student presses Ctrl+Shift+I (or Cmd+Option+I on macOS) during a quiz with a `keyboard_shortcut` rule configured
- **THEN** the default devtools-opening action is prevented and a `keyboard_shortcut` event is reported

#### Scenario: Menu-opened devtools remains undetected
- **WHEN** a student opens developer tools via the browser's menu (no keyboard shortcut involved)
- **THEN** no `keyboard_shortcut` event is reported — this remains a documented, accepted limitation since no DOM event fires for menu-driven browser actions
