## Context

The repository has development tooling only. `docker-compose.yml` mounts source, runs uvicorn with `--reload`, fronts S3 with LocalStack and hardcodes `postgres/postgres`. `docker/app/Dockerfile` installs `.[dev]` as an editable package. `railway.json` is a leftover from a platform we are not using. The one CI workflow, `runner-image.yml`, builds the standalone check runner and pushes it to GHCR on version tags.

Target host: a single Oracle Cloud A1.Flex instance, 2 OCPU / 2GB RAM, 64-bit ARM (Ampere), block storage only.

Four properties of the existing application make this design simpler than it would otherwise be, and each was verified in the code rather than assumed:

1. **HTTP state is externalised.** Authentication is a signed JWT in a cookie (`api/routes/auth.py`); quiz progress, per-question timers and violation counts live in `quiz_attempts` rows, with remaining time derived from `started_at` (`api/routes/student_quiz.py`). Nothing is held in process memory between requests, so replicas need no sticky routing and no shared session store.
2. **Background jobs are already multi-instance safe.** Both scheduled jobs take PostgreSQL advisory locks before doing work — `outbox_processor` on lock id 7919 and `teacher_digest_processor` on its own id. Running two replicas does not double-send notifications.
3. **Checks are effectively serialised.** Check execution is dispatched from the outbox processor, which is advisory-locked and awaits each dispatch in turn. At most one sandbox container runs at a time in practice, which is what makes a 2GB host viable.
4. **Base images are all multi-arch.** `python:3.12-slim`, `docker:27-cli`, `postgres:16-alpine`, MinIO, Caddy and Watchtower all publish `linux/arm64`. Current subject plugins use `python:3.12-slim`.

The one property that works against us: `run_migrations()` calls `alembic upgrade head` with no coordination, so two replicas booting together race on DDL.

## Goals / Non-Goals

**Goals:**

- A single-file production stack that an operator can bring up on a fresh host with a domain name and an env file.
- Deploys triggered by pushing to `main`, with no inbound access to the host and no host credentials in CI.
- A student cannot lose an in-flight quiz request to a deploy.
- Everything fits in 2GB with headroom for a check sandbox.
- A disk failure is recoverable.

**Non-Goals:**

- Multi-host orchestration, Swarm or Kubernetes. One VM.
- Percentage-based canary releases and automated metric-driven promotion. There is no metrics pipeline to promote against, and a canary keeps two versions live *longer*, which works against the mid-quiz consistency goal.
- Autoscaling. Replica count is fixed at two.
- Zero-downtime for the database. A PostgreSQL restart is an outage; it is also a rare, operator-initiated event.
- Centralised log aggregation. Docker's json-file driver with rotation is sufficient at this size.

## Decisions

### Rolling replacement over blue-green

Watchtower in `--rolling-restart` mode replaces one labelled container at a time. Caddy load-balances across the replicas; while one is being replaced the other serves everything.

The alternative considered was true blue-green: two complete stacks with an atomic proxy flip driven by a deploy script invoked over SSH from GitHub Actions. It gives a genuinely instantaneous cutover and single-command rollback, which is stronger. It was rejected because it requires storing an SSH private key to the production host in GitHub secrets and requires the host to accept inbound connections from GitHub's runners. Rolling replacement keeps the update pull-initiated: the host reaches out, nothing reaches in. On a 2GB host the memory argument reinforces this — blue-green needs both stacks resident during the swap.

The cost is a mixed-version window of roughly a minute, which is what forces the expand/contract migration rule below.

### Watchtower is label-scoped, never global

`WATCHTOWER_LABEL_ENABLE=true`, and only the app service carries `com.centurylinklabs.watchtower.enable=true`. An unscoped Watchtower would happily pull a new `postgres:16-alpine` at three in the morning and restart the database under live traffic. Data services are updated deliberately, by an operator, during a maintenance window.

### Caddy dynamic upstreams with passive health checks

Caddy's `dynamic a` upstream module re-resolves the `app` service name against Docker's embedded DNS on an interval, so replacing a replica (and with it, its container IP) needs no Caddy reload.

Dynamic upstreams are incompatible with Caddy's *active* health checking, so health is passive: a replica that refuses a connection is marked down for `fail_duration` and requests are retried elsewhere via `lb_retries`. This is not a downgrade for our purposes — it is precisely the behaviour we want. Retries fire on connection-level failures, meaning the request never reached the application, so retrying a `POST /quiz/{id}/submit` cannot double-submit. A request that is accepted and then errors is returned to the client untouched.

### Draining via stop grace period

Docker's default `stop_grace_period` is 10 seconds, after which the container is killed. uvicorn handles SIGTERM by closing its listening socket and letting in-flight requests finish, so the grace period is exactly the drain budget. It is set to 60s: long enough for a slow grading path, short enough that a rollout of two replicas does not drag on.

Combined with the retry behaviour above, the handoff has two independent guarantees — requests already inside the old replica finish there, and requests that arrive after its socket closes are retried onto the live replica.

### Migrations stay in application startup, guarded by an advisory lock

The obvious alternative is a one-shot `migrate` service that compose runs to completion before the app starts. It was rejected as the primary mechanism because Watchtower has no concept of it: Watchtower pulls a new image and restarts containers, and cannot be made to run a separate job first. A pre-update lifecycle hook does not help either, since it executes inside the *old* container and would therefore run the old code's migrations.

Keeping migrations in `lifespan` composes correctly with rolling replacement: the first replacement replica applies the migration, the second finds nothing to do. The race is closed by wrapping the upgrade in `pg_advisory_lock` (the blocking variant, not `pg_try_advisory_lock` — a replica that loses the race must wait and then proceed, not skip migrating). Session-scoped advisory locks are released automatically if the holder dies, so a replica killed mid-migration does not wedge the next boot.

A `migrate` profile is still provided for bootstrap and for manually applying a long migration ahead of a release.

**Consequence, and it is the sharpest edge in this design:** during a rollout both versions run against the migrated schema, so migrations must be backward-compatible within a release. Additive DDL is safe. Dropping or renaming something the previous release still reads is not, and must be split expand-then-contract across two releases. This is documented in `docs/deployment.md` and stated as a requirement in the `zero-downtime-release` spec.

### Two replicas, not three

Two is the minimum that makes rolling replacement meaningful, and the 2GB budget does not comfortably hold three. Watchtower's rolling restart stops a container before starting its replacement, so peak memory is two replicas, not three — there is no overlap spike to budget for. During the moment of replacement the stack is at one replica, which the host can serve at this traffic level.

Approximate steady-state budget: OS and dockerd ~180MB, Caddy ~20MB, PostgreSQL ~350MB with `shared_buffers=256MB`, MinIO capped at 256MB, Watchtower ~15MB, two replicas at ~300MB each. That is roughly 1.4GB, leaving ~600MB for a transient sandbox at its 256MB default. Swap is configured on block storage with `vm.swappiness=10` as a cushion against spikes, not as working memory.

### MinIO on-box rather than external object storage

Cloudflare R2 was considered and would free ~200MB and remove object storage from the host's backup burden entirely. MinIO was chosen to keep the deployment self-contained — no external account, no third-party credential, and proctoring snapshot uploads do not acquire a dependency on outbound network health during an exam. The budget above shows it fits. The trade is that MinIO's data now shares the host's disk and must be covered by backups, which is why `deployment-backups` mirrors the bucket alongside the database dump.

### Non-root application user with supplementary group access to the socket

The application launches check sandboxes by invoking `docker run` against the mounted host socket. Socket access is root-equivalent on the host, and this is unchanged from the current development setup — but the production image should not compound it by also running as uid 0. The image creates an unprivileged user; compose grants socket access with `group_add: ["${DOCKER_GID}"]`, where `DOCKER_GID` is the host's Docker group id. It is host-specific, so it belongs in the env file rather than the image.

The sandbox's existing host-path constraints are unchanged and carry over: `/tmp` is mounted at the same path inside and out so the daemon can resolve the output directory, and `HOST_PLUGINS_DIR` is the host-absolute plugins path.

### Sandbox resource requests become bounded

`check_core` currently passes a subject's declared `memory` and `cpus` straight through to `docker run`. On a 24GB host a subject declaring `memory: 2g` is untidy; on a 2GB host it is an outage. New settings clamp the request and log when clamping occurs. Clamping rather than rejecting keeps a misconfigured subject running in degraded form instead of failing every submission, and the log line tells the operator and the subject author what happened. The cap lives in `check_core` so the standalone runner inherits it.

### Multi-architecture images from one workflow

`docker/build-push-action` with `platforms: linux/amd64,linux/arm64` and QEMU emulation publishes a single manifest that serves ARM to the host and x86-64 to contributors. Emulated ARM builds are slow; GitHub Actions layer caching keeps this tolerable, and if build time becomes a problem the escape hatch is a native ARM runner rather than dropping an architecture.

### Two tags per build

`:main` moves with the branch and is what Watchtower polls. `:sha-<short>` is immutable and is what an operator pins to when rolling back. Rollback pins the image and stops the updater from immediately re-pulling the faulty `:main`. Note that pinning the image does not revert the schema — which is safe only because of the expand/contract rule, and the runbook says so explicitly.

## Risks / Trade-offs

- **Mixed-version window during rollout** → Expand/contract migrations are a spec requirement, documented in the deployment guide, and the two-release removal sequence is spelled out. This is a discipline the team must keep; it is not enforced by tooling.
- **2GB is genuinely tight** → Hard `mem_limit` on every service, a sandbox memory cap, `shared_buffers` and `max_connections` tuned down, bounded per-replica connection pools, and swap as a cushion. The headroom calculation is written down so it can be re-checked when a service is added.
- **A long migration blocks the replica's boot** → The replica stays unhealthy while migrating and Watchtower may time out waiting for it. Mitigation: apply long migrations manually via the `migrate` profile before the release lands, which the runbook covers.
- **A check sandbox is killed when its replica is replaced** → The submission's outbox message was never committed as finished, so it is retried by the surviving replica. Worst case is a re-run, not a stuck submission. Orphaned sandbox containers are cleaned by `--rm` and the existing timeout kill path.
- **Docker socket access is root-equivalent** → Accepted, and unchanged from today. Mitigated only by running the app unprivileged inside the container and by the operational rule that this host runs nothing else of value. Worth revisiting with a rootless or socket-proxy approach later.
- **Emulated ARM builds are slow** → Layer caching; escape hatch is a native ARM runner.
- **Watchtower could pull a broken image and roll it out unattended** → The pipeline's quality gates are the guard: a build that fails lint, types or tests is never pushed, so `:main` only ever advances to a build that passed. Residual risk is a defect the tests do not catch, answered by the documented rollback.
- **Single host is a single point of failure** → Out of scope to solve; addressed to the extent it can be by scheduled, verified-restorable backups.
- **Let's Encrypt rate limits during repeated rebuilds** → Caddy's certificate storage is on a named volume so certificates survive container replacement and are not re-requested on every deploy.

## Migration Plan

1. Provision the host: Docker Engine, swap on block storage, `vm.swappiness=10`, firewall limited to 80/443/SSH.
2. Point the domain's A record at the host.
3. Create `/opt/submissions-checker/.env` from `.env.prod.example`, `chmod 600`. Generate `SECRET_KEY` with `openssl rand -hex 32`; set `DOCKER_GID` from the host's Docker group.
4. If the GHCR package is private, authenticate the host with a read-scoped token and mount that config into Watchtower.
5. Bring up data services first, run the `migrate` profile to bootstrap the schema, then start the full stack.
6. Verify: certificate issued, login works over HTTPS, a submission runs a check end to end, a proctoring snapshot lands in MinIO.
7. Verify zero-downtime behaviour explicitly: drive continuous requests through the proxy, trigger a rollout, confirm no request fails.
8. Verify the backup path by restoring a dump onto a scratch database.
9. Remove `railway.json`.

**Rollback:** pin `APP_IMAGE_TAG` to the previous `:sha-<short>`, stop Watchtower, recreate the app service. The schema is not rolled back; this is safe because the release before it is schema-compatible by construction.

## Open Questions

- Is the GHCR package private or public? Private is the safer default and requires a read-scoped pull token on the host; public removes host-side registry credentials entirely. The bootstrap documentation will cover both, but the stack should ship configured for one.
- Where do backups go? The initial implementation writes to a second block volume on the same host, which protects against volume loss but not host loss. Off-host replication is a follow-up.
