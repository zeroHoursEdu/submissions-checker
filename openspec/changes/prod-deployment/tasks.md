## 1. Application changes that the deployment depends on

- [x] 1.1 Wrap `alembic upgrade head` in `core/migrations.py` with a blocking PostgreSQL advisory lock (its own lock id, distinct from 7919 and the digest lock), acquired on a dedicated connection and released in a `finally` block
- [x] 1.2 Add a test proving two concurrent `run_migrations()` calls serialize and both return successfully with the schema at head
- [x] 1.3 Add a test proving the advisory lock is released when the migration raises, so a subsequent call can acquire it
- [x] 1.4 Add `sandbox_max_memory` and `sandbox_max_cpus` settings to `core/config.py` with documented defaults suited to a 2GB host
- [x] 1.5 Clamp the subject-declared `memory`/`cpus` to those maxima in `services/check_core.py`, logging subject, requested value and applied value when clamping occurs
- [x] 1.6 Add tests covering: an over-limit request is clamped and warned, an under-limit request passes through unchanged and unwarned, and the clamp applies on the standalone runner path

## 1b. Snapshot evidence must not be public before it reaches the internet

- [x] 1b.1 Add an authenticated `GET` endpoint that streams a snapshot by id, calling `require_subject_access` for the owning subject before returning any bytes
- [x] 1b.2 Add a `download_bytes` method to `StorageService` and stop setting `ACL="public-read"` on upload
- [x] 1b.3 List evidence in the teacher view by snapshot id and render links to the application endpoint instead of the object-storage URL
- [x] 1b.4 Stop returning a publicly fetchable URL from the snapshot upload response
- [x] 1b.5 Add tests: owner teacher gets the image, a non-owning teacher gets 403, a student is refused, an anonymous request is refused, and uploads carry no public ACL

## 2. Production image

- [x] 2.1 Restructure `docker/app/Dockerfile` as multi-stage with an explicit production target: runtime dependencies only (`--no-dev`), non-editable install
- [x] 2.2 Copy the Docker client from `docker:27-cli` instead of installing apt `docker.io`
- [x] 2.3 Create an unprivileged runtime user, `chown` the application directory, and drop to it
- [x] 2.4 Replace the curl-based healthcheck with a dependency-free Python one against `/health`, with a `start_period` covering migration and boot
- [x] 2.5 Keep the development target working — `docker compose up` with reload must behave exactly as before
- [x] 2.6 Verify the built image locally: it runs, it launches a sandbox, and it contains no test or lint tooling

## 3. Production compose stack

- [x] 3.1 Create `docker-compose.prod.yml` with `caddy`, `app` (2 replicas, no `container_name`), `postgres`, `minio`, `watchtower`, and a `migrate` service behind a profile
- [x] 3.2 Give every service a hard `mem_limit` matching the design's budget, and tune `postgres` with `shared_buffers=256MB` and `max_connections=30`
- [x] 3.3 Set `stop_grace_period: 60s` on `app`, and bound the per-replica database pool so total connections stay under `max_connections`
- [x] 3.4 Publish ports on `caddy` only; place everything else on an internal network with no host port bindings
- [x] 3.5 Mount a shared named volume at `/app/uploads` in every replica, so a submission received by one replica is readable by the replica that processes it
- [x] 3.6 Mount `/var/run/docker.sock`, `/tmp` at the same path inside and out, and the host-absolute plugins directory into `app`; set `group_add: ["${DOCKER_GID}"]`
- [x] 3.7 Label `app` with `com.centurylinklabs.watchtower.enable=true` and confirm no other service carries the label
- [x] 3.8 Configure `watchtower` with `WATCHTOWER_LABEL_ENABLE=true`, `WATCHTOWER_ROLLING_RESTART=true`, `WATCHTOWER_CLEANUP=true` and a 60s poll interval
- [x] 3.9 Add named volumes for PostgreSQL data, MinIO data and Caddy's certificate storage; configure json-file log rotation on every service
- [x] 3.10 Write `docker/caddy/Caddyfile`: automatic HTTPS for `{$DOMAIN}`, `dynamic a` upstreams resolving `app` on a short refresh, passive health checks with `fail_duration`/`max_fails`, and `lb_retries` for connection-level failures only
- [x] 3.11 Write `.env.prod.example` listing every required variable — `DOMAIN`, `SECRET_KEY`, database credentials, MinIO credentials, `DOCKER_GID`, `HOST_PLUGINS_DIR`, `APP_IMAGE_TAG`, AI provider keys — with generation instructions and no real values

## 4. Release pipeline

- [x] 4.1 Add `.github/workflows/app-image.yml` triggered on push to `main` and on pull requests targeting `main`
- [x] 4.2 Run `make quality` and the test suite as gates ahead of the build, so a failing build is never published
- [x] 4.3 Build with `docker/build-push-action` for `linux/amd64,linux/arm64` using QEMU and GitHub Actions layer caching
- [x] 4.4 Push `ghcr.io/<owner>/submissions-checker:main` and `:sha-<short>` on `main`; build only, never push, on pull requests
- [x] 4.5 Confirm the ARM variant builds and runs (verified locally under emulation: builds, non-root, Docker CLI present, app imports); confirm the *published* manifest carries both architectures after the first push to `main`

## 5. Backups

- [x] 5.1 Add a backup service that runs `pg_dump` on a schedule and writes timestamped dumps to a backup volume
- [x] 5.2 Mirror the MinIO bucket to the same destination on the same schedule
- [x] 5.3 Prune artifacts older than a configurable retention window
- [x] 5.4 Make a failed backup exit non-zero and log the failure rather than failing silently
- [x] 5.5 Perform a real restore from an actual backup artifact onto a scratch database and record the verified procedure

## 6. Documentation

- [x] 6.1 Write `docs/deployment.md`: host bootstrap (Docker, swap, `vm.swappiness`, firewall), DNS, secrets file creation, GHCR authentication for a private package, first-boot sequence
- [x] 6.2 Document the expand/contract migration rule with the concrete two-release sequence for removing a column, and state why it is required
- [x] 6.3 Document the rollback runbook: pin `APP_IMAGE_TAG` to a `:sha-` tag, stop Watchtower, recreate — and state plainly that the schema is not rolled back
- [x] 6.4 Document backup and restore for both PostgreSQL and MinIO
- [x] 6.5 Record the memory budget with per-service figures so it can be re-checked whenever a service is added
- [x] 6.6 Link the deployment guide from `README.md` and delete `railway.json`

## 6b. Release visibility and deployment tracking

- [x] 6b.1 Bake the source revision into the production image at build time and expose it on an unauthenticated version endpoint
- [x] 6b.2 Create a GitHub Release for each published build, tagged immutably, naming the rollback image tag and digest
- [x] 6b.3 Open a GitHub Deployment for each published build and resolve it by polling the production host's version endpoint
- [x] 6b.4 Mark the deployment failed when the host does not report the published revision within the window
- [x] 6b.5 Skip deployment tracking cleanly when no production host is configured
- [x] 6b.6 Document the release and deployment tabs, and the repository variable that enables tracking

## 7. Verification on the target host

- [ ] 7.1 Bring the stack up on the A1 host; confirm the certificate is issued and login works over HTTPS
- [ ] 7.2 Run a submission end to end; confirm the sandbox launches, `result.json` is read back, and grading persists
- [ ] 7.3 Upload a proctoring snapshot; confirm it lands in MinIO, is retrievable by an authorized teacher, and is NOT reachable directly from the internet
- [x] 7.4 Verified locally against the real stack: 22,718 requests across 10 concurrent streams through Caddy while both replicas were replaced 8s apart — zero failures, zero non-200. Re-run on the A1 host once provisioned
- [x] 7.5 Verified locally: containers carry `StopTimeout=60`, and a replaced replica logs a full lifespan shutdown (`application_shutting_down` → `application_shutdown_complete`) rather than being killed
- [x] 7.6 Verified locally: a `--run-once --debug` pass logs `Only checking containers using enable label` and examines only the two app replicas; postgres, minio and caddy never appear
- [x] 7.7 Measured on the full stack: ~495M actual against ~1540M of limits (app 95M per replica, postgres 29M idle, minio 64M, caddy 15M). Figures recorded in docs/deployment.md; re-measure under real load on the host
- [ ] 7.8 Exercise the rollback runbook end to end on the real host
