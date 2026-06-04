# Quiz Anti-Cheat System

Anti-cheat settings are configured per quiz template. Everything is stored in the `anti_cheat` key inside the quiz `config` JSONB. No database schema changes are needed to add or modify rules.

---

## Overview

The system detects suspicious browser events during a quiz and responds according to teacher-configured rules. On each violation the student sees a specific warning message. Depending on the rule action, the quiz may be auto-failed, the timer reduced, or the attempt flagged for teacher review.

**What it can detect:**

| Event | Browser API | Signals |
|---|---|---|
| `tab_switch` | `visibilitychange` → hidden | Student opened another tab or switched apps |
| `window_blur` | `window.blur` | Browser window lost focus (alt+tab, clicked another window) |
| `resize` | `window.resize` ≥ 150 px | Window resized — may indicate split-screen with notes |
| `copy_attempt` | `document.copy` | Student pressed Ctrl+C or used browser copy |
| `keyboard_shortcut` | `keydown` | Ctrl+C/A/S/P, F12, PrintScreen |
| `right_click` | `contextmenu` | Right-click (search, translate, inspect) |
| `fullscreen_exit` | `fullscreenchange` | Student exited forced-fullscreen mode |

**What it cannot reliably prevent:**
- Using a second device (phone, tablet) to look up answers
- Taking a photo of the screen
- Screen-reading tools / accessibility software
- Browser extensions that intercept or override JavaScript
- OS-level clipboard operations before the quiz page is focused

---

## Config Structure

The full config lives inside the quiz template's `config` JSONB:

```json
{
  "total_questions": 10,
  "time_limit_minutes": 30,
  "max_quiz_attempts": 3,
  "pass_threshold_pct": 0.6,
  "shuffle_questions": true,
  "shuffle_options": true,
  "show_correct_answers_after": false,

  "anti_cheat": {
    "text_protection": true,
    "disable_right_click": true,
    "require_fullscreen": false,
    "rules": [
      {
        "event": "tab_switch",
        "threshold": 3,
        "action": {
          "type": "fail",
          "message": "You switched tabs too many times. Quiz auto-failed."
        }
      },
      {
        "event": "resize",
        "threshold": 2,
        "action": {
          "type": "reduce_time",
          "penalty_seconds": 60,
          "message": "Window resize detected. {penalty_seconds}s deducted from your time."
        }
      }
    ]
  }
}
```

### `anti_cheat` top-level fields

| Field | Type | Default | Description |
|---|---|---|---|
| `text_protection` | boolean | false | Disables text selection (`user-select: none`) and blocks the browser `copy` event |
| `disable_right_click` | boolean | false | Blocks the context menu |
| `require_fullscreen` | boolean | false | Requests Fullscreen API on quiz load; auto re-enters if exited (unless `fullscreen_exit` rule fires) |
| `rules` | array | [] | List of violation rules (see below) |

### Rule fields

| Field | Type | Required | Description |
|---|---|---|---|
| `event` | string | yes | Event type (see table above) |
| `threshold` | integer ≥ 1 | yes | Number of times the event must occur before the action fires |
| `action` | object | yes | What happens when threshold is reached (see below) |

### Action object

| Field | Type | Required for | Description |
|---|---|---|---|
| `type` | `"fail"` \| `"warn"` \| `"reduce_time"` \| `"flag"` | all | Action type |
| `message` | string | all (optional) | Text shown to the student. Supports template placeholders. |
| `penalty_seconds` | integer | `reduce_time` only | Seconds to deduct from the effective time limit |

#### Action types

| Type | Effect on student | Effect on grade |
|---|---|---|
| `fail` | Quiz auto-submitted immediately with a red error banner | Attempt status = `VIOLATION_FAIL`, graded as not passed |
| `warn` | Amber warning banner (dismisses after 7 s) | No grade impact; violation count recorded |
| `reduce_time` | Amber banner with new time; timer jumps back | No direct grade impact; may lead to timeout |
| `flag` | No visible message | Attempt flagged for teacher review (visible in the assignment grade table) |

**Note:** Each rule fires at most once per threshold crossing. If `threshold = 3` and student hits the event 5 times, the action fires when count reaches 3 and does not repeat at 4 or 5.

### Message placeholders

Use these inside the `message` string — they are substituted server-side before being sent to the browser:

| Placeholder | Resolves to |
|---|---|
| `{count}` | Number of times this event has occurred so far |
| `{threshold}` | The threshold configured in the rule |
| `{penalty_seconds}` | Seconds deducted (only meaningful for `reduce_time`) |
| `{fail_threshold}` | Threshold of the first `fail` rule for the same event (useful for warn messages: "X more will auto-fail") |

---

## Presets

The quiz editor UI offers three presets as a starting point:

### None
All rules cleared, no protections.

### Soft monitoring
```json
{
  "text_protection": true,
  "disable_right_click": true,
  "require_fullscreen": false,
  "rules": [
    {"event": "tab_switch",    "threshold": 5, "action": {"type": "warn",  "message": "Warning: you switched tabs {count} time(s). After {fail_threshold} this quiz will be auto-failed."}},
    {"event": "window_blur",   "threshold": 8, "action": {"type": "warn",  "message": "You left the quiz window {count} time(s)."}},
    {"event": "resize",        "threshold": 3, "action": {"type": "flag",  "message": ""}},
    {"event": "copy_attempt",  "threshold": 1, "action": {"type": "flag",  "message": ""}}
  ]
}
```

### Strict
```json
{
  "text_protection": true,
  "disable_right_click": true,
  "require_fullscreen": false,
  "rules": [
    {"event": "tab_switch",        "threshold": 3, "action": {"type": "fail",        "message": "You switched tabs too many times. Quiz auto-failed."}},
    {"event": "window_blur",       "threshold": 5, "action": {"type": "warn",        "message": "Warning: you left the quiz window {count} time(s). {fail_threshold} more will auto-fail."}},
    {"event": "resize",            "threshold": 2, "action": {"type": "reduce_time", "penalty_seconds": 60, "message": "Window resize detected — {penalty_seconds}s deducted from your time."}},
    {"event": "copy_attempt",      "threshold": 1, "action": {"type": "warn",        "message": "Copy attempt recorded and flagged."}},
    {"event": "keyboard_shortcut", "threshold": 3, "action": {"type": "reduce_time", "penalty_seconds": 30, "message": "Suspicious key detected — {penalty_seconds}s deducted."}},
    {"event": "fullscreen_exit",   "threshold": 1, "action": {"type": "fail",        "message": "You exited fullscreen mode. Quiz auto-failed."}}
  ]
}
```

---

## Teacher view

The assignment grade table (`/teacher/subjects/{id}/assignments/{id}`) includes a **Flags** column showing:

- **Auto-failed** (red badge) — attempt was terminated by `fail` action
- **N events** (amber badge) — sum of all recorded violation counts; no auto-fail yet
- **—** — no violations recorded

---

## How violations are stored

Each `quiz_attempt` row has a `violations` JSONB column. It stores:

```json
{
  "tab_switch": 2,
  "window_blur": 1,
  "_time_penalty_seconds": 60,
  "_force_fail": true,
  "_flagged_events": ["copy_attempt"]
}
```

| Key | Description |
|---|---|
| `<event_name>` (integer) | Running count for that event |
| `_time_penalty_seconds` | Total seconds deducted so far (affects server-side remaining time calculation) |
| `_force_fail` | Set to `true` when a `fail` action fires; the next page load or submit immediately ends the attempt |
| `_flagged_events` | List of event types that triggered a `flag` action |

The time penalty is applied server-side when calculating `seconds_remaining` on both page load and on every violation event response, making it impossible for a student to bypass by disabling JS.

---

## Limitations and honest caveats

1. **JS can be disabled.** A student who disables JavaScript before loading the quiz will not trigger any events. The server enforces the time limit but cannot detect behavioral events without JS. Consider coupling anti-cheat with short time limits for high-stakes assessments.

2. **Browser extensions.** Extensions can intercept DOM events and suppress them. There is no reliable defense against this.

3. **Fullscreen is advisory.** The Fullscreen API requires a user gesture on many browsers. On iOS Safari it is not supported. If `require_fullscreen` is enabled, a student on an unsupported browser will simply not enter fullscreen, and the `fullscreen_exit` rule may fire immediately.

4. **`window_blur` fires on legitimate interactions.** Clicking a notification, switching to a file picker, or interacting with the OS taskbar all fire `blur`. Keep the threshold high (≥ 5) if using this event.

5. **`resize` threshold.** Resize events fire continuously while the user drags the window border. The JS debounces this and only counts changes ≥ 150 px, but rapid back-and-forth may still generate multiple counts.
