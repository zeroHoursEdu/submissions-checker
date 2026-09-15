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
make observability-up    # prometheus:9090, grafana:3000 (anonymous admin), alloy:12345
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

## Adding a metric

1. Define it in `core/metrics.py` with a docstring that says what question it answers.
   Labels only from small closed sets (an enum, an outcome). No ids.
2. Increment or set it where the event happens (after commit) or in `metrics_refresh.py`.
3. Put it on a panel in `build_dashboards.py` — the test fails for an exported metric no
   panel reads.
4. `make dashboards-json`, run `pytest tests/unit/test_observability_assets.py
   tests/unit/test_metrics.py`, then `make dashboards`.
5. Re-check `estimate_series()`.
