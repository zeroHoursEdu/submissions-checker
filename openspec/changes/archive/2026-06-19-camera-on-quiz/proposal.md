## Why

Quizzes currently rely only on passive browser anti-cheat (tab-switch, blur, copy, fullscreen-exit). A student can still take a quiz with help off-screen, read answers from a phone, or have someone else present — none of which the browser can see. Teachers want real confidence that a quiz was taken honestly, accepting that a strict system may wrongly stop some honest students (who then sit a personal exam).

## What Changes

- Add **webcam proctoring** to quiz taking, with all ML running **client-side** (MediaPipe FaceLandmarker + TensorFlow.js COCO-SSD, loaded from CDN — no build tooling, no server GPU).
- Detect four conditions and emit them as new anti-cheat **event types**: `camera_face_absent`, `camera_multiple_faces`, `camera_looking_away` (gaze/head-pose, catches looking down at a phone), `camera_phone_detected` (phone/book object), plus `camera_blocked` (no/denied camera).
- Route these events through the **existing generic anti-cheat rule engine** unchanged — `report_violation` matches `anti_cheat.rules[]` by event name and applies `fail | reduce_time | warn | flag`. Strictness and punishment are therefore fully configurable per quiz.
- Add a `quiz.anti_cheat.camera` config block: `enabled`, `require_camera`, `on_no_camera`, `capture_snapshots`, `snapshot_on`, and per-detector tuning (`sustain_seconds`, thresholds, confidence). Strict-by-default examples: no camera blocks quiz start; phone = instant fail.
- On flagged events, **capture a webcam frame and store it to S3** as evidence (new `StorageService.upload_bytes`, new `quiz_attempt_snapshots` table). Teacher review shows violation counts + snapshot thumbnails so borderline cases can be judged for the personal exam.
- Add a **recording-consent legal notice**: new `recording_consent_at` on the student account; the portal/quiz is gated behind a one-time "I agree" consent screen on first login. A "you are being recorded" banner also shows on each proctored attempt.

## Capabilities

### New Capabilities
- `quiz-proctoring`: Webcam-based proctoring during quiz taking — client-side detection of face presence, multiple faces, gaze/head-pose, and phone/object; configurable strictness and punishment via the existing rule engine; S3 snapshot evidence; teacher-side review of violations and snapshots; camera-required start gate.
- `recording-consent`: One-time legal consent that a student may be recorded during proctored quizzes — recorded on the account on first login and required before quiz access.

### Modified Capabilities
<!-- None: the anti-cheat rule engine is reused as-is via new event-type strings; no existing spec-level behavior changes. -->

## Impact

- **Backend**: `api/routes/student_quiz.py` (new `POST /quiz/{id}/snapshot`; `show_quiz` passes camera config), `services/storage.py` (add `upload_bytes`), new model `db/models/quiz_attempt_snapshot.py`, `api/routes/teacher_portal.py` (surface snapshots), student account model + auth/portal gate for consent, new Alembic migration `0020`.
- **Frontend**: `templates/student_quiz.html` (proctoring JS block, camera preview, detection loop, snapshot capture — reuses existing `report()`/`showBanner()`), new consent screen template.
- **Config**: subject `config.yml` gains the `quiz.anti_cheat.camera` schema (snapshotted into `quiz_attempts.config_snapshot` at attempt start).
- **Dependencies**: CDN-loaded `@mediapipe/tasks-vision`, `@tensorflow/tfjs`, `@tensorflow-models/coco-ssd` (no npm). Heavier client first-load; weak devices may lag.
- **Privacy**: stores student webcam snapshots (PII) — mitigated by consent, capture-only-on-flag, and a retention follow-up.
- **No change** to the punishment rule engine, the outbox/scheduler, or the quiz grading flow.
