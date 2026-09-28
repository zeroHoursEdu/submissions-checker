# Observability

Metrics, dashboards and alerting. Design: `docs/superpowers/specs/2026-09-15-prometheus-grafana-observability-design.md`.

## What is measured and why

The app exposes `GET /metrics` (Prometheus text format) from a private registry defined in
`src/submissions_checker/core/metrics.py`. Grafana Alloy scrapes it and pushes the samples
outbound — into a throwaway Prometheus locally, into Grafana Cloud in production. Nothing
opens an inbound port for this; Caddy answers 404 for `/metrics`.

Two dashboards, one question each:

| Dashboard | Question | Grafana Cloud |
|---|---|---|
| **Submissions Checker — Technical** | Is the service healthy? | `https://charmingaphid2632.grafana.net/d/subchk-technical` |
| **Submissions Checker — Goals** | Do students use it? | `https://charmingaphid2632.grafana.net/d/subchk-goals` |

Both live in the Grafana Cloud folder **Submissions Checker** (uid `subchk`). The same
stack also hosts another project's dashboards in the General folder; nothing here touches
them, and every query filters on `job="submissions-checker"` so the two never mix.

Three kinds of metric:

- **HTTP** — counted by *route template* (`/portal/quiz/{attempt_id}`), never by raw
  path, so a scanner probing `/wp-admin` cannot create series. One latency histogram
  for all routes.
- **Events** — counters incremented where the thing happens: logins, quiz attempts
  started/finished/passed, answers, uploads, disputes, air-raid pauses, check / AI review /
  email outcomes, outbox outcomes. Incremented after the transaction commits.
- **State** — gauges recomputed from Postgres every 60s by a scheduled job
  (`workers/scheduled/metrics_refresh.py`): student counts, active students by window,
  backlogs, outbox depth and age. The job sets `app_db_healthy` to 0 when its queries
  fail, which is what the database alert watches.

Series budget: the Grafana Cloud free tier allows 10k active series shared with the other
project (~900). This app is estimated at ~380 for two replicas (`estimate_series()` in
`observability/grafana/build_dashboards.py`); a unit test keeps it under 500. That is why
there are no per-student, per-subject, per-question or per-route-latency series, and why
the GC collector and `_created` series are disabled.

## Local

```bash
make up                  # the app, as usual
make observability-up    # prometheus:9090, loki:3100, grafana:3000 (anonymous admin), alloy:12345
make observability-down
make alloy-logs          # scrape/push problems show up here
```

Grafana provisions the two dashboards from `observability/grafana/*.json` and the alert
rules from `observability/grafana/alerting/alert-rules.yaml` — the very files
`make dashboards` / `make alerting` push to Grafana Cloud. The datasource is provisioned
under the uid the hosted Prometheus has (`grafanacloud-prom`), so one JSON renders in
both places.

The dev image must contain `prometheus-client`; after pulling this change run
`docker compose build app` once.

## Production

Alloy runs as a compose service behind the `observability` profile. In the host `.env`:

```dotenv
COMPOSE_PROFILES=observability
GRAFANA_CLOUD_PROM_URL=https://prometheus-<region>.grafana.net/api/prom/push
GRAFANA_CLOUD_PROM_USER=<numeric instance id>
GRAFANA_CLOUD_PROM_TOKEN=<access-policy token, scope metrics:write>
SUBCHK_ENV=prod
```

The three values come from grafana.com → your stack → Prometheus → *Send Metrics*; the
token is created under *Administration → Cloud access policies*. Then the usual
`docker compose -f docker-compose.prod.yml --env-file .env up -d` starts Alloy alongside
the rest. Confirm:

```bash
docker compose -f docker-compose.prod.yml --env-file .env logs --tail 50 alloy
#   no "401"/"403" → the token works
#   no "could not resolve app" → the replicas are up and on the edge network
```

and the Technical dashboard's *Replicas up* stat reads 2 within a minute.

Alloy has a 96m memory limit, taken from the sandbox headroom (see the budget comment in
`docker-compose.prod.yml`). Without the profile line it is not started and costs nothing.

Alloy's `instance` label is the replica's container IP, which changes on every rollout;
dashboards aggregate with `sum`/`max` so nothing visible changes, and the stale series
age out of the active count.

## Dashboards and alerts

The JSON and YAML are **generated**. Edit the scripts, regenerate, and the unit test
`tests/unit/test_observability_assets.py` checks that the committed files match, that
every query names a metric the app really exports, and that every exported metric is on
some panel.

```bash
make dashboards-json    # build_dashboards.py + build_alerting.py → committed files
make dashboards         # push both dashboards to Grafana Cloud
make alerting           # push the Telegram contact point, policy route and 4 rules
```

Both pushes read the laptop `.env`:

```dotenv
GRAFANA_URL=https://charmingaphid2632.grafana.net
GRAFANA_API_TOKEN=      # Administration → Users and access → Service accounts → Editor role
TELEGRAM_BOT_TOKEN=     # from @BotFather
TELEGRAM_CHAT_ID=       # send the bot a message, then GET https://api.telegram.org/bot<TOKEN>/getUpdates → chat.id
```

`push.py` creates the folder `subchk` if missing (and refuses to continue if that uid
exists with another title), upserts the two dashboards into it, upserts the contact point
`subchk-telegram`, adds one route to the notification policy (`app=submissions-checker` →
that contact point, keeping every other route as it was) and upserts the rule group
`subchk`. Objects are pushed with `X-Disable-Provenance`, so thresholds can still be
tuned in the Grafana UI; the next push overwrites them, so put lasting changes in the
script.

### The four rules

| Rule | Fires when | For |
|---|---|---|
| ServiceDown | `max(up) < 1`, **or no data at all** (Alloy or the host died) | 3m |
| HighErrorRate | 5xx > 5% of requests over 10m, with at least 20 requests | 5m |
| DbUnhealthy | `app_db_healthy == 0` on every replica | 2m |
| OutboxStuck | oldest PENDING outbox row older than 15 minutes | 5m |

All carry the label `app=submissions-checker` and go to Telegram through the route above.
Resolved notifications are on. The local Grafana loads the same rules (no contact point),
so they can be watched going Pending → Firing by stopping Postgres: `docker compose stop
postgres`, wait about two minutes, `docker compose start postgres`.

## Logs

Design: `docs/superpowers/specs/2026-09-28-quiz-forensics-logging-design.md`.

### Pipeline

Every container writes structured JSON to stdout (`LOG_FORMAT=json`, the compose
default outside `ENVIRONMENT=development`). The same Alloy that scrapes metrics reads
them straight off the Docker socket — `discovery.docker` finds the containers,
filtered to this compose project by the `SUBCHK_COMPOSE_PROJECT` env var (so a
worktree's stack never mixes with another checkout's), and `loki.source.docker`
tails them. Alloy mounts `/var/run/docker.sock:ro` for this: root-equivalent access to
the socket is already held by the app and Watchtower, and Alloy publishes no port, so
this adds one pinned-version, non-exposed container to that trust boundary rather than
a new one.

In production this pushes to Grafana Cloud Loki (`GRAFANA_CLOUD_LOKI_URL/USER/TOKEN`,
templated in `.env.prod.example` right after the `GRAFANA_CLOUD_PROM_*` block, same
access-policy pattern, scope `logs:write`). Locally it pushes to a throwaway `loki`
container in the `observability` profile (`make observability-up`, which resolves and
passes the real compose project name so this works from a worktree too).

Only `level` is pulled out of the JSON body into a Loki label (via `stage.json` +
`stage.labels`); everything else — `request_id`, `user_id`, `attempt_id`,
`submission_id`, `event`, … — stays in the body and is read at query time with
`| json`. Loki also auto-derives `detected_level` and `service_name` labels
(`discover_log_levels`/`discover_service_name`, on by default); they mirror
`level`/`service` 1:1 and add no extra streams, but Grafana's Logs Drilldown view
expects `service_name` to exist, so they're kept rather than disabled.

Non-JSON stdout (Postgres, MinIO) still arrives in Loki unlabelled by `level` — it's
there for grepping, just not level-filterable. All container logs ship, including
Postgres error lines whose `DETAIL` can contain row values (e.g. the email in a
duplicate-key violation) — they land in Grafana Cloud with the same ~14-day retention.

### Labels

`app="subchk"`, `service` (the compose service name), `env` (`prod` or `local`),
`level`. Deliberately not more than that: an id-per-stream label (e.g. `attempt_id`)
would create one Loki stream per attempt, which is what free-tier stream-cardinality
limits are for. Every id lives in the JSON body instead and is filtered with `| json`.

### Searching locally

The dev compose default is `LOG_FORMAT=console` (human-readable, not JSON), so local
Loki has nothing to parse with `| json` unless you turn it on:

```bash
LOG_FORMAT=json docker compose up -d app
# or add LOG_FORMAT=json to .env and `docker compose up -d app`
```

### Event-name contract

Names are load-bearing: the dashboards, alerts and
`tests/unit/test_observability_assets.py` all key off the literal string.

| Event | Level | Key fields |
|---|---|---|
| `quiz_attempt_started` / `quiz_attempt_resumed` | info | `attempt_id`, `student_id`, `assignment_id`, `squad_id`, `question_count` |
| `quiz_answer_saved` | info | `attempt_id`, `question_id` |
| `quiz_questions_expired` | info | `attempt_id`, `burned` |
| `quiz_anticheat_event` | info (warning if `action != none`) | `attempt_id`, `event_type`, `count_after`, `action`, `outcome`, `rule_threshold`, `away_ms` |
| `quiz_attempt_force_failed` | warning | `attempt_id`, `student_id`, `event_type`, `count_after`, `rule_threshold`, `violations` |
| `quiz_attempt_finished` | info (warning if `status=VIOLATION_FAIL`) | `attempt_id`, `student_id`, `status` (`COMPLETED`/`TIMED_OUT`/`VIOLATION_FAIL`), `score`, `max_score`, `passed`, `duration_s` |
| `quiz_snapshot_saved` / `quiz_snapshot_failed` | info / error | `attempt_id`, `event_type` |
| `quiz_dispute_created` / `quiz_dispute_resolved` | info | `dispute_id`, `question_id`, `accepted` |
| `outbox_dispatched` | info | `outbox_id` (bound), `event_type`, `attempt_no` |
| `outbox_failed` | error (with traceback) | same, plus `submission_id`/`attempt_id` when the payload carries one |
| `outbox_dead` | error | same, `retries` |
| `check_started` / `check_finished` | info | `submission_id` (bound), `status`, `score`, `duration_ms` |
| `ai_review_finished` | info | `submission_id` (bound), `code_mark`, `provider`, `duration_ms` |
| `ai_review_failed` | error (with traceback) | `submission_id` (bound), `provider` |
| `http_request` | info / warning (4xx≠401,404) / error (5xx) | `request_id`, `user_id`, `role`, `method`, `route`, `status`, `duration_ms` |

Every request line carries `request_id` (echoed back as the `X-Request-ID` response
header) and, once auth has run, `user_id`/`role`. Background work binds its own
context instead: the outbox processor binds `outbox_id`/`event_type`/`attempt_no`
(plus `submission_id`/`attempt_id` when the message payload has one) for the whole
dispatch; scheduled jobs bind `job`. Never logged: request body, query string,
cookies, or any header but the request id.

### Dashboards and investigating a disputed fail

Two more dashboards beyond the Prometheus pair, same `subchk` folder, also pushed by
`push.py` — but never made public/anonymous, unlike the Prometheus pair (which stay
self-contained, no template variables, so a public link is safe). These two carry
`user_id`/`attempt_id` template variables and show who did what, so they must stay
behind login:

| Dashboard | Question |
|---|---|
| **Submissions Checker — Quiz investigation** (`subchk-quiz-investigation`) | What happened during this attempt (or this student)? |
| **Submissions Checker — Logs** (`subchk-logs`) | Is anything failing right now? |

To investigate a disputed auto-fail: start on the teacher page,
`/teacher/quiz-attempts/{attempt_id}/events` (linked from the auto-fail badge in the
assignment table and from the submission review page's proctoring block) — the DB
timeline (`quiz_attempt_events`) is kept for the life of the attempt, permanently, and
is the source of truth for "what rule fired and why." Only reach for the **Quiz
investigation** dashboard (filter by `attempt_id` or `user_id`) for the surrounding
technical context (was the browser slow, did other requests fail around the same
time) — Loki's free tier keeps roughly 14 days, so it is not where old evidence lives.

Alerts (`make alerting`, existing Telegram contact point, prod only):

- **QuizForceFailSpike** — more than 5 auto-fails across all students in 10 minutes.
  Usually means a rule or a browser change is producing false positives, not five
  separate cheaters.
- **OutboxDead** — any message exhausts its retries in 15 minutes.

### Example LogQL

```logql
{app="subchk", env="prod", service="app"} | json | request_id="<id>"
{app="subchk", env="prod", service="app"} | json | user_id="<id>" | event="http_request"
{app="subchk", env="prod", service="app"} | json | event="quiz_attempt_force_failed"
```

### Retention

- **DB timeline** (`quiz_attempt_events`): forever — this is the record a teacher
  rules a dispute from.
- **Loki** (Grafana Cloud free tier): ~14 days. Good for "why did this request 500
  last Tuesday," not for a two-month-old ban.

## Adding a metric

1. Define it in `core/metrics.py` with a docstring that says what question it answers.
   Labels only from small closed sets (an enum, an outcome). No ids.
2. Increment or set it where the event happens (after commit) or in `metrics_refresh.py`.
3. Put it on a panel in `build_dashboards.py` — the test fails for an exported metric no
   panel reads.
4. `make dashboards-json`, run `pytest tests/unit/test_observability_assets.py
   tests/unit/test_metrics.py`, then `make dashboards`.
5. Re-check `estimate_series()`.
