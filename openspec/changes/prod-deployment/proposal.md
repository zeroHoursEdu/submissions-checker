## Why

The project has no production deployment path. `docker-compose.yml` is development-only (bind mounts, `--reload`, LocalStack, hardcoded `postgres/postgres` credentials), `docker/app/Dockerfile` ships dev dependencies and an editable install, and `railway.json` points at a platform we are not using. Deploying today means stopping the single app container and waiting ~30s for migrations and boot — a window in which a student pressing "submit quiz" gets a 502 and loses an attempt.

We need a self-hosted production stack on a single Oracle A1.Flex ARM VM (2 OCPU / 2GB) that redeploys itself when `main` is pushed, without ever dropping an in-flight quiz request.

## What Changes

- **New `docker-compose.prod.yml`** — Caddy (automatic HTTPS, load balancer), 2 replicas of the app, PostgreSQL, MinIO, Watchtower, and a backup sidecar. Hard `mem_limit` on every service so a runaway check sandbox cannot OOM the database.
- **Zero-downtime rolling deploys** — Watchtower polls GHCR and performs `--rolling-restart`, updating one app replica at a time. Caddy routes around the replica being replaced; the replaced replica drains in-flight requests before exiting.
- **Watchtower is label-scoped.** Only app replicas carry `com.centurylinklabs.watchtower.enable=true`. PostgreSQL, MinIO and Caddy are never auto-updated.
- **New `.github/workflows/app-image.yml`** — on push to `main`: run quality gates and tests, then build a multi-arch (`linux/amd64,linux/arm64`) image and push `ghcr.io/<owner>/submissions-checker:main` and `:sha-<short>`. amd64 keeps the image usable on x86 development machines; arm64 is what the VM runs.
- **Production Dockerfile target** — multi-stage build, `--no-dev` dependency install, Docker CLI copied from `docker:27-cli` instead of apt `docker.io`, non-root runtime user with `group_add` for socket access, and a dependency-free Python healthcheck.
- **Advisory-locked migrations** — `run_migrations()` acquires a PostgreSQL advisory lock so concurrent replica boots serialize instead of racing Alembic DDL.
- **BREAKING (operational, not API): migrations must be backward-compatible within a release.** During a rolling deploy the old and new image versions serve traffic against the same migrated schema. Destructive DDL (drop/rename column) must be split across two releases using expand/contract.
- **New `sandbox_max_memory` setting** — caps the `memory` a subject's `config.yml` may request. Today a plugin can declare `memory: 2g` and exhaust the host.
- **Backups** — scheduled `pg_dump` of PostgreSQL and mirroring of the MinIO bucket, so a single disk failure is not total data loss.
- **Proctoring snapshots become authenticated.** Evidence frames are currently written public-read and rendered as direct object-storage URLs. That is confined to LocalStack today; deploying would put students' webcam images on the internet, readable by anyone holding the URL. Snapshots move behind an authenticated application endpoint and MinIO is never exposed.
- **Shared upload storage.** `uploads/` is a container-local directory. With two replicas, a ZIP received by one replica is invisible to the other, which is the one that may process it. It becomes a shared volume.

## Capabilities

### New Capabilities
- `production-deployment`: The production runtime topology — service composition, TLS termination, load balancing, replica health gating, resource limits, and the secrets/configuration contract for the host.
- `zero-downtime-release`: Guarantees around releasing a new version while requests are in flight — rolling replacement, connection draining, retry-on-refused, migration compatibility rules, and rollback.
- `release-pipeline`: The CI/CD contract — what triggers a build, what gates it must pass, the image naming and multi-arch requirements, and how the running deployment discovers a new image.
- `deployment-backups`: Scheduled backup and restore expectations for PostgreSQL and object storage.

### Modified Capabilities
- `check-runner`: Sandbox memory requested by a subject's `config.yml` becomes bounded by a configurable host maximum rather than unbounded.
- `quiz-proctoring`: Snapshot evidence stops being publicly readable and is served only through an authenticated, authorization-checked endpoint.

## Impact

**New files**
- `docker-compose.prod.yml`, `docker/caddy/Caddyfile`, `docker/backup/` (backup sidecar entrypoint)
- `.github/workflows/app-image.yml`
- `.env.prod.example`
- `docs/deployment.md` (host bootstrap, secrets, expand/contract migration rule, rollback runbook)

**Modified files**
- `docker/app/Dockerfile` — production multi-stage target
- `src/submissions_checker/core/migrations.py` — advisory lock
- `src/submissions_checker/core/config.py` — `sandbox_max_memory`
- `src/submissions_checker/services/check_core.py` — enforce the cap
- `src/submissions_checker/services/storage.py` — drop public-read ACL, add authenticated object download
- `src/submissions_checker/api/routes/teacher_portal.py` — authenticated snapshot endpoint; evidence listed by id
- `src/submissions_checker/api/routes/student_quiz.py` — upload no longer returns a public URL
- `templates/teacher_assignment.html` — evidence links address the application
- `README.md` — link the deployment guide
- `railway.json` — removed; superseded by this stack

**Unaffected by design**
- Outbox and teacher-digest schedulers already hold PostgreSQL advisory locks, so running two replicas needs no change.
- Auth is a stateless JWT cookie and quiz state lives entirely in `quiz_attempts`, so no sticky sessions or shared session store are required.

**Operational dependencies**
- A domain with an A record pointing at the VM (required for Caddy to issue certificates).
- A GHCR pull credential on the host if the package is private.
- `DOCKER_GID` matching the host's `docker` group, for socket access from the non-root app user.

**Accepted risk**
- The app container mounts the Docker socket to launch check sandboxes, which is root-equivalent on the host. This is unchanged from the existing development setup, but it means the VM should run nothing else of value.
