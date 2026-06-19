## Context

Quiz taking already has a generic, config-driven anti-cheat system. `report_violation` (`student_quiz.py:436`) is generic over the event-type string: it increments `quiz_attempts.violations[event]`, matches `config_snapshot.anti_cheat.rules[]` by event name, and applies `fail | reduce_time | warn | flag`. The frontend (`student_quiz.html`, lines 206–298) reports events via a single `report(type)` fetch helper and renders results with `showBanner()`.

The frontend is server-rendered Jinja2 + Tailwind-via-CDN with **no build tooling** (no npm/webpack/vite); all JS is inline in templates. There is an S3-compatible `StorageService` (`services/storage.py`) used for subject images, exposing `upload_file(local_path, key)`. Background work uses a transactional outbox + APScheduler (not Celery). Students authenticate via JWT cookie → `User` → `Student`; they receive credentials by email (`SEND_CREDENTIALS` outbox flow).

This change adds webcam proctoring by plugging new detectors into that existing seam, plus snapshot evidence storage, teacher-side display, and a recording-consent gate.

## Goals / Non-Goals

**Goals:**
- Detect face-absent, multiple-faces, looking-away (gaze/head-pose), and phone/object — fully client-side.
- Reuse the existing punishment engine unchanged; strictness/punishment configured per quiz.
- Capture snapshot evidence to S3 on flagged events; surface to teachers.
- Gate quiz access behind a one-time recording-consent acknowledgement.
- Strict-by-default config (no camera blocks start; phone = instant fail).

**Non-Goals:**
- Server-side ML / tamper-proof proctoring (client ML is bypassable — accepted trade-off).
- Continuous video recording or streaming (only flagged still frames are stored).
- Per-user gaze calibration (use head-pose thresholds instead).
- Changes to grading, outbox/scheduler, or the rule-engine logic itself.

## Decisions

**Client-side ML via CDN (MediaPipe FaceLandmarker + TF.js COCO-SSD).**
FaceLandmarker gives face count and a facial transformation matrix → head yaw/pitch for gaze, with no calibration; COCO-SSD detects `cell phone`/`book`. Both load from CDN as ES modules, matching the existing Tailwind-CDN, no-build approach. *Alternative:* server-side ML (more tamper-resistant) — rejected for GPU cost, S3 traffic, infra, and privacy; left as a future option.

**New event types through the existing endpoint/engine — zero engine changes.**
Detections map to `camera_*` event strings sent to `POST /quiz/{id}/event`. Because the engine matches rules by string, strictness/punishment is pure config. *Alternative:* a parallel camera-specific pipeline — rejected as needless duplication.

**Two-layer thresholding.** Client debounce (`sustain_seconds`) turns noisy per-frame detections into discrete events; the existing rule `threshold` then governs how many events trigger an action. Keeps the server engine simple and stateless about ML noise.

**Detector scheduling.** Face/gaze run at ~3–5 fps on a downscaled frame; COCO-SSD (heavy) runs every few seconds (`phone.sample_seconds`). Avoids pegging the CPU and keeps the quiz responsive.

**Config nested under `quiz.anti_cheat.camera`.** Keeps camera tuning beside the `rules[]` it feeds, and rides the existing `config_snapshot` mechanism so in-progress attempts are isolated from config edits.

**Snapshot storage = new table + `upload_bytes`.** `upload_file` only takes a local path; add `StorageService.upload_bytes(data, key, content_type)` reusing the existing client/URL logic. A dedicated `quiz_attempt_snapshots` table (indexable, one row per frame) is cleaner than stuffing image refs into `violations` JSONB. New `POST /quiz/{id}/snapshot` (multipart) reuses the ownership + `IN_PROGRESS` guards from `report_violation` and validates size/content-type. *Alternative:* embed the frame in the `/event` call — rejected to keep event reporting lightweight and frequent.

**Consent on the student account, gated at the portal.** Add nullable `recording_consent_at`; a one-time consent screen on first login stamps it and blocks quiz access until set. Account-level (not per-quiz) so it is asked once. Notice text is configurable for jurisdiction. A per-attempt "you are being recorded" banner is the belt-and-suspenders reminder.

## Risks / Trade-offs

- **Student PII (stored webcam frames)** → consent gate + capture only on flagged actions (`snapshot_on`) + documented retention; add an S3 lifecycle/retention follow-up (link `security_followups`).
- **Client ML is bypassable** (disable JS, fake camera) → accepted; goal is raising the bar and flagging for the in-person exam, not proof. `camera_blocked` + `require_camera` catch the obvious "no camera" case.
- **Performance on weak devices** (two in-browser models) → staggered sampling on downscaled frames; strict-by-design intent tolerates some friction.
- **Heavy first-load download** (model/wasm assets) → "loading proctoring…" state before the form is interactive; consistent with existing CDN-asset approach.
- **False positives** (poor lighting, head movement) → tunable `sustain_seconds`/thresholds per quiz, and lenient actions (`warn`/`flag`) available; teacher review + personal exam is the safety valve.

## Migration Plan

- Additive Alembic `0020`: create `quiz_attempt_snapshots`; add `recording_consent_at` to the student account table. No backfill (null = not yet consented).
- No change to existing quizzes: absent/disabled `camera` config = current behavior. Rollback = `downgrade -1` (table + column drop); feature is dormant without config.

## Open Questions

- Exact CDN versions to pin for MediaPipe/TF.js (resolve during apply; pin explicitly).
- Snapshot retention period and whether to enforce via S3 lifecycle now or as a follow-up.
- Whether the consent screen lives on the student account model or the `User` model (resolve by locating the first-login flow during apply).
