# Quiz forensics + searchable logs — design

Date: 2026-09-28. Status: approved in brainstorming, awaiting spec review.

## Problem

Students report that quizzes "work badly" and some were auto-failed by anti-cheat. Today it is
impossible to tell whether a fail was deserved or a false positive:

- `report_violation` (`api/routes/student_quiz.py`) writes no log line at all.
- The DB keeps only per-type counters (`quiz_attempts.violations = {"tab_switch": 3,
  "_force_fail": true}`): no timestamps, no rule that fired, no client context.
- The client sends only the event name. A 200 ms `window_blur` from an OS notification and a
  40 s absence look identical.
- Application logs go to container stdout only (json-file driver, 10 MB rotation) and are not
  shipped anywhere, so they cannot be searched and are gone within days.

## Goals

1. For any quiz attempt, a teacher can see a permanent, ordered timeline of anti-cheat events
   with the rule/action applied and enough client context to judge intent.
2. All container logs are shipped to Loki (Grafana Cloud on prod, a local Loki in dev) and are
   structured so they can be filtered by `request_id`, `user_id`, `attempt_id`, `submission_id`.
3. Ready-made Grafana dashboards and log-based alerts make the common investigations one click.

## Non-goals

- Changing anti-cheat rules, thresholds, scoring or grading. Counters and rule evaluation stay
  byte-for-byte the same; the timeline is written alongside them.
- A client-side JS error endpoint (considered, deferred).
- Tracing / OTLP.

## Decisions (from brainstorming)

| Question | Decision |
|---|---|
| Where ban evidence lives | Both: DB timeline (permanent, shown to teacher) + Loki (14-day free-tier retention, technical debugging) |
| Client context | Yes, a whitelisted, size-capped `ctx` object per event |
| Logging review scope | Request context + access log; background task context; quiz lifecycle events + `except` audit |
| Log collection | Alloy reads container stdout via the Docker socket (`loki.source.docker`) |

## 1. Anti-cheat timeline

### Table `quiz_attempt_events` (migration 0033)

| column | type | notes |
|---|---|---|
| `id` | BIGINT identity PK | |
| `attempt_id` | BIGINT FK → `quiz_attempts.id` ON DELETE CASCADE, indexed | |
| `event_type` | VARCHAR(64) | already validated by `_EVENT_TYPE_RE` |
| `count_after` | INTEGER NULL | counter after this event; NULL for informational and ignored events |
| `rule_threshold` | INTEGER NULL | threshold of the rule that matched |
| `action` | VARCHAR(16) | `none`, `warn`, `reduce_time`, `flag`, `fail` |
| `outcome` | VARCHAR(32) | `applied`, `informational`, `ignored_paused`, `ignored_not_in_progress`, `ignored_type_cap` |
| `client_ctx` | JSONB NULL | sanitized client context |
| `ip` | VARCHAR(64) NULL | first `X-Forwarded-For` hop, else socket peer — reuse the helper in `core/rate_limit.py` |
| `user_agent` | VARCHAR(256) NULL | truncated |
| `created_at` / `updated_at` | `TimestampMixin` | |

`outcome` is a native PG enum `quiz_event_outcome` backed by `QuizEventOutcome` in
`db/models/enums.py` (UPPERCASE values, per convention). `action` stays a plain string: its
values (`none`, `warn`, …) are the config vocabulary teachers write in `config.yml`.

Row cap: at most 500 rows per attempt. Beyond it events still update counters as today but are
not stored; `quiz_anticheat_event` logs still record them. The cap is checked with an indexed
`COUNT(*)` on `attempt_id` per event (at most 500 rows; events arrive a few per minute), so
`violations` gains no bookkeeping key.

Write semantics: the row is added in the same transaction as the `attempt.violations` update.
For early-return paths (not in progress, paused, type cap) a row is still written with the
matching `ignored_*` outcome, then the existing response is returned unchanged.

### Informational events

`tab_return` and `focus_return` are reserved types. They carry `away_ms`, are stored with
`outcome=INFORMATIONAL`, never increment `violations`, never count toward
`_MAX_DISTINCT_EVENT_TYPES`, and never match a rule even if a config names them (the handler returns before the rule loop;
config apply has no anti-cheat validation today and none is added).

### Client context (`_quiz_anticheat.html`)

`report(type, ctx)` posts `{"type": ..., "ctx": {...}}`. Fields:

- every event: `visibility` (`visible|hidden`), `has_focus` (bool), `vw`, `vh` (ints),
  `ms_since_load` (int)
- `resize`: `from_w`, `from_h`, `to_w`, `to_h`
- `tab_return` / `focus_return`: `away_ms`
- `camera_*`: `faces` (int), `yaw`, `pitch` (degrees, -180..180, `camera_looking_away` only),
  `held_ms` (the gate's sustain window)
- `keyboard_shortcut`: `combo` (e.g. `ctrl+c`, `f12`; modifiers + the one key, never text)

`_read_event_type` becomes `_read_event(request) -> tuple[str, dict]`. Sanitizer
(`services/quiz_events.py::sanitize_ctx`): keeps only whitelisted keys, ints clamped to
`[0, 10**9]`, `yaw`/`pitch` to `[-180, 180]`, bools, `visibility` from its enum, `combo`
matching `^((ctrl|alt|meta|shift)\+){0,4}[a-z0-9]{1,12}$`. Invalid or oversized
(`> 1 KB` serialized) ctx is dropped to `{}` — never a 400; the event must be recorded. The
existing 413/400 rules for the body and `type` are unchanged.

### Teacher UI

The existing review page (`/teacher/submissions/{id}/review`) only opens for
`AWAITING_TEACHER_REVIEW` submissions, so an auto-failed quiz never reaches it. The timeline
therefore gets its own page, `GET /teacher/quiz-attempts/{attempt_id}/events`
(`teacher_quiz_attempt_events.html`), linked from: the violation / auto-failed badge in the
assignment table (`teacher_assignment.html`), and each attempt in the review page's proctoring
block. The page shows the attempt header (student, status, score, started/submitted) and a
table: offset from attempt start
(`+04:12`), event, count, action (`fail` in red), human-readable context (`не було 42 с`,
`1920×1080 → 1920×640`, `облич: 0`), and a grey "ігноровано (пауза)" marker for ignored rows.
Strings in `i18n/uk.yml`. Access: subject owner or ADMIN via `require_subject_access`
(attempt → submission → student_assignment → subjects_assignment → subject); unknown attempt
404, another teacher's attempt 403.

## 2. Application logging

### Format

New setting `log_format: Literal["json", "console"]`, default `console` when
`ENVIRONMENT=development`, else `json`. Prod compose sets `json` explicitly; dev compose passes
`${LOG_FORMAT:-console}`. Both renderers see the same processors.

stdlib/uvicorn records go through `structlog.stdlib.ProcessorFormatter` with the same
`foreign_pre_chain`, so every stdout line is one JSON object in `json` mode. Uvicorn access log
stays off (replaced by `http_request`).

### Request context — `core/request_logging.py`

Pure ASGI middleware, added directly inside `PrometheusMiddleware`:

- on entry: `structlog.contextvars.clear_contextvars()`, bind `request_id` (incoming
  `X-Request-ID` if it matches `^[A-Za-z0-9-]{8,64}$`, else `uuid4().hex[:16]`), `method`,
  `route` (matched route template; `unmatched` otherwise)
- `_get_current_user` binds `user_id` and `role` after successful auth
- on exit: one `http_request` event with `status`, `duration_ms`. Level: 5xx → `error`;
  4xx except 401/404 → `warning`; else `info`. Skipped for `/health*`, `/metrics`, `/static/*`
- response header `X-Request-ID` always set
- never logged: body, query string, cookies, headers other than the request id, email, name

### Background context

- `outbox_processor`: per message bind `outbox_id`, `event_type`, `attempt_no`, and
  `submission_id` / `attempt_id` when present in the payload. Events: `outbox_dispatched`,
  `outbox_failed` (with traceback; there is no backoff — the message is re-picked on the next
  10 s tick), `outbox_dead` (retries exhausted). Idle ticks log at debug.
- `check_tasks`: `check_started`, `check_finished` (`status`, `score`, `duration_ms`).
- `review_tasks`: `ai_review_finished` (`code_mark`, `provider`, `duration_ms`),
  `ai_review_failed`.
- scheduler jobs: bind `job` for each run; clear contextvars at start.

### Quiz lifecycle events (names are a contract — dashboards and tests depend on them)

| event | level | key fields |
|---|---|---|
| `quiz_attempt_started` / `quiz_attempt_resumed` | info | `attempt_id`, `student_id`, `assignment_id`, `squad_id`, `question_count` |
| `quiz_answer_saved` | info | `attempt_id`, `question_id` |
| `quiz_questions_expired` | info | `attempt_id`, `burned` |
| `quiz_anticheat_event` | info (warning when action ≠ none) | `attempt_id`, `event_type`, `count_after`, `action`, `outcome`, `rule_threshold`, `away_ms` |
| `quiz_attempt_force_failed` | warning | `attempt_id`, `student_id`, `event_type`, `count_after`, `rule_threshold`, `violations` |
| `quiz_attempt_finished` | info (warning for `VIOLATION_FAIL`) | `attempt_id`, `student_id`, `status` (`COMPLETED`/`TIMED_OUT`/`VIOLATION_FAIL`), `score`, `max_score`, `passed`, `duration_s` |
| `quiz_snapshot_saved` / `quiz_snapshot_failed` | info / error | `attempt_id`, `event_type` |
| `quiz_dispute_created` / `quiz_dispute_resolved` | info | `dispute_id`, `question_id`, `accepted` |

### `except` audit

Every `except Exception` / bare broad `except` in `src/` either re-raises or logs with
`logger.exception` (or `warning` with the exception when expected) plus the identifying ids.
The implementation plan lists each location.

## 3. Pipeline and Grafana

### Alloy (`observability/alloy/config.alloy`, shared by prod and local)

- `discovery.docker` on `unix:///var/run/docker.sock`, filtered by label
  `com.docker.compose.project` = `SUBCHK_COMPOSE_PROJECT` env
- `discovery.relabel` → labels `app="subchk"`, `service` (compose service), `env`
- `loki.source.docker` → `loki.process`:
  - `stage.json` extracting `level` (app and caddy both log JSON; non-JSON lines such as
    postgres pass through unlabelled); `stage.labels` for `level` only. Ids stay in the JSON body and are read with `| json` at query time — no
    structured metadata, which would collide with the `| json` names (`attempt_id_extracted`)
    and buys nothing at this volume
- `loki.write` → `GRAFANA_CLOUD_LOKI_URL` with basic auth `GRAFANA_CLOUD_LOKI_USER` /
  `GRAFANA_CLOUD_LOKI_TOKEN`

Compose (both files): alloy mounts `/var/run/docker.sock:ro`. Security note: socket access is
root-equivalent on the host; the app and watchtower already hold it, and alloy publishes no
ports, so the attack surface grows by one pinned-version, non-exposed container. Prod alloy
`mem_limit` 96M → 128M; the memory table in `docker-compose.prod.yml` is recomputed
(committed ~1716M → ~1748M, headroom ~320M → ~288M).

**Risk: memory headroom.** Current headroom is ~320M against a 384m sandbox ceiling, i.e.
already overcommitted by design (ceilings, not reservations). Raising alloy by 32M is accepted;
the header comment records the new total. If alloy's steady RSS stays under 96M after a week in
prod, the limit is reverted.

### Local

- `loki` service (`grafana/loki`, pinned version, single-binary, filesystem storage, 24h
  retention, config `observability/local/loki.yml`), profile `observability`
- datasource `grafanacloud-logs` (same uid as the hosted Loki) added to
  `observability/local/grafana-datasource.yml`
- `make observability-up` / `observability-down` include `loki`
- `docs/observability.md` explains `LOG_FORMAT=json` for local log search

### Dashboards (`build_dashboards.py`)

`DS_LOGS = {"type": "loki", "uid": "grafanacloud-logs"}`; every LogQL selector starts with
`{app="subchk", env="$env"}`.

- `subchk-quiz-investigation.json` — variables `env`, `attempt_id`, `student_id` (textbox).
  Panels: logs panel of `quiz_anticheat_event` for the attempt; events by type over time;
  table of `quiz_attempt_force_failed` in range (attempt_id, student_id, event, count, rule);
  all `http_request` lines for `user_id=$student_id`; warnings/errors carrying the attempt id.
- `subchk-logs.json` — error/warning rate by `logger`; top error patterns (`| pattern`);
  5xx by `route`; slow requests (`duration_ms > 2000`); `outbox_failed` / `outbox_dead`;
  `check_finished` with non-success status; `ai_review_failed`.

### Alerts (`build_alerting.py`, existing Telegram contact point)

- `quiz_attempt_force_failed` count > 5 in 10 min (prod) — a rule or a browser change is
  probably producing false positives
- `outbox_dead` count > 0 in 15 min (prod)

### Test for assets

`tests/unit/test_observability_assets.py` is extended: every `event="..."` literal in LogQL of
the generated dashboards and alerts must appear as a string literal in `src/`; generated JSON
must match the builder output (existing drift check covers new files).

## 4. Prod rollout

1. User creates a Grafana Cloud access-policy token with `logs:write` and provides the Loki
   push URL and user id. Secrets never enter git.
2. New `scripts/ops/prod-compose.sh` (thin wrapper over `prod_compose` in `_prod.sh`, with
   `--help`) is used to set `GRAFANA_CLOUD_LOKI_*`, `LOG_FORMAT=json` and
   `SUBCHK_COMPOSE_PROJECT` in the prod `.env` and to recreate alloy.
3. Take a backup first (`run-prod-backup` skill), because the release carries migration 0033.
4. Merge → CI image → Watchtower rolls app replicas; the first new replica applies 0033 at
   startup (additive table, so it satisfies the backward-compatible migration rule in
   `docs/deployment.md`) → recreate alloy
   → `make dashboards && make alerting`.
5. Verify: Explore `{app="subchk", env="prod"}` shows lines from app/caddy/postgres; a test
   student quiz on prod produces `quiz_anticheat_event` and a timeline row visible to the
   teacher.

`docs/deployment.md`, `docs/observability.md`, `docs/commands.md`, `.env.example` and
`scripts/ops/prod.env.example` are updated.

## 5. Testing

- unit: `sanitize_ctx` (whitelist, clamps, combo regex, oversize → `{}`); middleware level
  selection and request-id validation; `LOG_FORMAT` switching
- integration: `/event` writes a timeline row with correct `action`/`outcome` for each rule
  action, for paused, not-in-progress, type-cap, 500-row cap and informational events;
  counters and responses are unchanged versus current behaviour (regression)
- functional: `X-Request-ID` echoed/generated; `http_request` log contains no query string;
  teacher timeline visible to subject owner and admin, 403 for another teacher;
  `quiz_attempt_force_failed` emitted (structlog capture)
- assets: dashboard/alert event names exist in `src/`
- manual: `make observability-up` with `LOG_FORMAT=json`, run a quiz with tab switches, see
  the investigation dashboard populate
