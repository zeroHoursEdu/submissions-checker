## 1. Data model & migration

- [x] 1.1 Add `db/models/quiz_attempt_snapshot.py` — `quiz_attempt_snapshots` (id, attempt_id FK→quiz_attempts CASCADE, event_type, s3_key, s3_url, captured_at) + relationship on `QuizAttempt`; register in models package
- [x] 1.2 Locate the student account/first-login model; add nullable `recording_consent_at` (DateTime, tz-aware) — on `Student`
- [x] 1.3 Write Alembic `0020_add_quiz_proctor_snapshots_and_consent.py` (create table + add column); verify `upgrade head` and `downgrade -1` are clean — verified live (up → down → up) against throwaway Postgres

## 2. Storage service

- [x] 2.1 Add `StorageService.upload_bytes(data, key, content_type)` reusing existing aioboto3 client/URL logic
- [x] 2.2 Unit test `upload_bytes` (key, content-type, returned URL) — `tests/unit/test_proctoring.py` with a fake S3 client

## 3. Snapshot endpoint

- [x] 3.1 Add `POST /portal/quiz/{attempt_id}/snapshot` (multipart frame) in `student_quiz.py` — reuse ownership + `IN_PROGRESS` checks from `report_violation`; validate size + image content-type
- [x] 3.2 On valid upload: store frame via `upload_bytes` to key `proctoring/attempt-{id}/{seq}-{event}.jpg`, insert `quiz_attempt_snapshots` row, return JSON
- [~] 3.3 Tests: success, 403 non-owner, reject when not IN_PROGRESS, reject oversized/non-image — NOT added as integration tests; repo has no authed-student httpx harness (quiz flow is covered by the Playwright e2e suite). Endpoint guards mirror the already-tested `report_violation`. Covered by manual E2E (9.4).

## 4. Pass camera config to the quiz page

- [x] 4.1 In `show_quiz()` pass `proctoring_config = config_snapshot["anti_cheat"]["camera"]` into the template context alongside `anti_cheat_config`

## 5. Frontend proctoring (student_quiz.html)

- [x] 5.1 Add `{% if proctoring_config and proctoring_config.enabled %}` block: hidden `<video>` + `<canvas>`, "you are being recorded" notice, "loading proctoring…" gate
- [x] 5.2 Load MediaPipe `@mediapipe/tasks-vision@0.10.14` and `@tensorflow/tfjs@4.22.0` + `@tensorflow-models/coco-ssd@2.2.3` from pinned CDN versions
- [x] 5.3 Camera permission request + start gate: on deny/unavailable with `require_camera`, report `camera_blocked` and block the form per `on_no_camera`
- [x] 5.4 Detection loop: FaceLandmarker (face count + yaw/pitch) at ~4 fps; COCO-SSD every `phone.sample_seconds` on the video frame
- [x] 5.5 Per-detector debounce by `sustain_seconds`; emit `camera_face_absent` / `camera_multiple_faces` / `camera_looking_away` / `camera_phone_detected` via a `report()` helper hitting the existing `/event` endpoint
- [x] 5.6 On a flagged action with `capture_snapshots` and action in `snapshot_on`, grab a canvas frame and POST to the snapshot endpoint

## 6. Teacher review

- [x] 6.1 In `teacher_portal.py` (existing `violation_flags` query, ~line 380) also load `quiz_attempt_snapshots` per attempt
- [x] 6.2 Render snapshot thumbnails in the assignment template (alongside the existing violation breakdown)

## 7. Recording consent

- [x] 7.1 Add a consent screen template (`student_consent.html`) with the configurable notice text
- [x] 7.2 Add routes to show the notice (`GET /portal/consent`) and a POST that stamps `recording_consent_at = now`
- [x] 7.3 Gate the portal/quiz: students with null `recording_consent_at` are redirected to the consent screen; consenting students are not re-prompted
- [x] 7.4 Make notice text configurable — `Settings.recording_consent_notice`

## 8. Config schema & example

- [x] 8.1 Add a proctored-quiz example (`quiz.anti_cheat.camera` + strict `rules`) to `plugins/e2e_test` lab2 `config.yml`

## 9. Tests & verification

- [~] 9.1 `report_violation` cases for the new `camera_*` event names — NOT added; the rule engine is generic over the event string and unchanged, so existing `report_violation` behavior already applies to camera event names. Covered by manual E2E (9.4).
- [~] 9.2 Consent-gate test — NOT added as integration test (no authed-student httpx harness). Covered by manual E2E (9.4).
- [x] 9.3 Migration verified live (`upgrade head` → `downgrade -1` → `upgrade head`) on a fresh Postgres; unit suite green (27 passed). Full integration/e2e suite needs testcontainers (Docker) — not run here.
- [ ] 9.4 Manual E2E per the plan (human-in-the-loop; requires a real webcam): deny camera → blocked; leave frame → face_absent; phone → fail; second face → multiple_faces + snapshot; teacher sees thumbnails.

> Note: `[~]` = intentionally deferred to manual E2E (9.4) rather than fabricated automated coverage, because the repo has no authed-student integration harness and the rule engine these events ride on is unchanged.
