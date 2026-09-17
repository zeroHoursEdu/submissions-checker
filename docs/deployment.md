# Production deployment

Self-hosted stack for a single VM. Deploys are triggered by pushing to `main`: GitHub
Actions publishes an image, and the host's Watchtower notices and replaces the app
replicas one at a time, so a student mid-quiz never sees a failed request.

Nothing in CI can reach this host. There is no SSH key in GitHub secrets, and the host
needs no inbound access beyond ports 80 and 443.

**Target host:** Oracle Cloud A1.Flex (Ampere ARM64), 2 OCPU / 2GB RAM, block storage.

---

## Contents

- [How a deploy works](#how-a-deploy-works)
- [Releases and deployment tracking](#releases-and-deployment-tracking)
- [First-time host setup](#first-time-host-setup)
- [The migration rule](#the-migration-rule-read-this-before-writing-one)
- [Creating the first account](#creating-the-first-account)
- [Rollback](#rollback)
- [Backups and restore](#backups-and-restore)
- [Memory budget](#memory-budget)
- [Operations](#operations)
- [Known exposure and accepted risk](#known-exposure-and-accepted-risk)

---

## How a deploy works

1. You push to `main`.
2. `.github/workflows/app-image.yml` runs lint, type checks and the full test suite. A
   failure stops here and nothing is published — this gate is what makes unattended
   deployment safe.
3. It builds a multi-architecture image (`linux/amd64` + `linux/arm64`) and pushes two
   tags: `:main` (moves with the branch) and `:sha-<short>` (immutable).
4. Watchtower on the host polls the registry every 60 seconds and sees a new digest for
   `:main`.
5. It replaces the app replicas **one at a time**. While replica 1 is restarting,
   Caddy routes everything to replica 2.

6. CI opens a GitHub Deployment and polls `https://<your domain>/version` until the
   host reports the new commit, then marks the deployment successful. If the host
   never reports it, the deployment goes red — see below.

Two independent mechanisms keep requests from being dropped during step 5:

- **Draining.** `stop_grace_period: 60s` gives uvicorn time to finish requests already
  in progress after it stops accepting new ones. A quiz submission being graded when
  its replica is replaced completes normally. (Docker's default is 10 seconds, which is
  not enough — that is why it is set explicitly.)
- **Retry on refusal.** Caddy retries a request that was refused at the connection
  level onto another replica. Refused means the request never reached the application,
  so retrying a `POST` cannot double-submit. A request the app accepted and then failed
  is returned to the client untouched, never retried.

The first replacement replica applies any pending migrations during startup, guarded by
a PostgreSQL advisory lock so concurrent boots serialize. The rest find nothing to do.

---

## Releases and deployment tracking

**Releases tab.** Every published build gets a release tagged `app-v<date>-<run>`,
listing the commits it contains, the image digest, and the exact `APP_IMAGE_TAG`
to set to roll back to it. Pick a rollback target by reading the releases rather
than by digging through workflow logs.

The tag prefix is `app-v`, deliberately distinct from the `runner-v` tags that
publish the subject runner image — those trigger a different workflow.

**Deployments tab.** Publication is not deployment. The host pulls on its own
schedule, and a stalled updater is a failure mode this project has already hit
once (Watchtower silently stopped deploying when its Docker API version went
unpinned). So CI does not call a build deployed just because it was pushed.

Instead the app reports the commit it was built from at `GET /version`:

```json
{"revision": "24681d36..."}
```

The revision is baked into the image at build time and cannot be overridden by
the host's `.env` — a value the host could set would tell you nothing. CI polls
that endpoint after publishing and requires several consecutive responses to
carry the new revision, because mid-rollout one replica still answers with the
old one. Only then does the deployment turn green.

If the host never picks the build up, the deployment turns **red** while the
build itself stays green, which is the honest split: the image is fine, the
rollout did not happen. Start with:

```bash
docker compose -f docker-compose.prod.yml logs watchtower
```

**Enabling it.** Set a repository variable (not a secret — it is a public URL):

> Settings → Secrets and variables → Actions → Variables → New repository variable
> `PRODUCTION_URL` = `https://submissions.example.edu`

Without it the tracking job skips and publishing is unaffected, which is what you
want before the host exists.

---

## First-time host setup

### 1. Provision

```bash
# Docker Engine
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # log out and back in

# Swap on block storage — a cushion against bursts, not working memory.
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swap.conf
sudo sysctl --system
```

Open only 80, 443 and SSH. On Oracle Cloud this means **both** the VCN security list and
the instance firewall — Oracle images ship with iptables rules that silently drop
traffic the security list allows:

```bash
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

### 2. DNS

Point an `A` record at the host's public IP **before** starting the stack. Caddy proves
control of the domain to obtain a certificate; if DNS is not yet live, issuance fails
and retries count against Let's Encrypt's rate limits.

### 3. Configuration

```bash
sudo mkdir -p /opt/submissions-checker
cd /opt/submissions-checker
git clone <this repo> .

cp .env.prod.example .env
chmod 600 .env
```

Fill in every value. Generate secrets rather than inventing them:

```bash
openssl rand -hex 32   # SECRET_KEY
openssl rand -hex 24   # POSTGRES_PASSWORD
openssl rand -hex 24   # MINIO_ROOT_PASSWORD

getent group docker | cut -d: -f3   # DOCKER_GID
```

`HOST_PLUGINS_DIR` must be a **host-absolute** path (e.g.
`/opt/submissions-checker/plugins`). The Docker daemon resolves the sandbox's bind
mounts on the host, so a path that only exists inside the container will not resolve.

Create it and give it to the container's user. The application image runs as the
unprivileged `app` user, **uid 10001**, and a bind mount keeps its host ownership —
so a directory made with `sudo mkdir` is owned by root and the app cannot write to
it. Applying a subject config then fails with `[Errno 13] Permission denied:
/app/plugins/.tmp-<subject>-...`, after the subject has already been committed to the
database.

```bash
sudo mkdir -p /opt/submissions-checker/plugins
sudo chown -R 10001:10001 /opt/submissions-checker/plugins
```

The same applies to `BACKUP_DIR`, which the backup container writes to as root, and
does *not* apply to `uploads`, which is a named volume and inherits the image's
ownership automatically.

### 4. Registry access

If the GHCR package is **private**, authenticate the host so Watchtower and `docker
pull` can read it. The token needs only `read:packages`:

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GITHUB_USER" --password-stdin
```

If the package is **public**, skip this; the mounted config is simply unused.

### 5. First start

```bash
# The backup image is built on the host, not pulled. Doing it first keeps it out
# of the way of the rest of the bring-up.
docker compose -f docker-compose.prod.yml build backup

# Data tier first, then bootstrap the schema explicitly, then everything else.
docker compose -f docker-compose.prod.yml up -d postgres minio minio-init
docker compose -f docker-compose.prod.yml --profile migrate run --rm migrate
docker compose -f docker-compose.prod.yml up -d
```

Every other service is pulled; `backup` is the one exception, because it is two
packages on top of Alpine and not worth a registry. If you pre-pull images by
hand, note that plain `docker compose pull` will report it as `Skipped` rather
than trying to fetch a `submissions-checker-backup` repository that does not
exist.

### 6. Verify

```bash
docker compose -f docker-compose.prod.yml ps          # all healthy

# GET, not HEAD: these routes are registered GET-only, so `curl -I` answers a
# correct-but-confusing 405 with `allow: GET`.
curl -s https://$DOMAIN/health        # {"status":"healthy"}
curl -s https://$DOMAIN/health/ready  # {"status":"ready","database":"connected"}
curl -s https://$DOMAIN/version       # the commit this host is running
```

Then, in a browser: log in, submit an assignment, confirm the check runs, and confirm a
proctoring snapshot is viewable from the teacher's assignment view.

If `COMPOSE_PROFILES=observability` is set, also confirm metrics leave the host:

```bash
docker compose -f docker-compose.prod.yml --env-file .env logs --tail 50 alloy   # no 401/403
```

and that *Replicas up* on the Technical dashboard reads 2 (see `docs/observability.md`).

---

## The migration rule (read this before writing one)

**Migrations must be backward-compatible within a release.**

During a rolling deploy, the old and new versions run *at the same time* against the
*same* migrated schema, for roughly a minute. If a migration drops or renames something
the still-running previous version reads, that version starts throwing errors mid-deploy
— for real students, mid-quiz.

Safe in one release:

- Adding a table
- Adding a **nullable** column, or one with a default
- Adding an index (`CONCURRENTLY` for a large table)
- Widening a type

Never in the same release as the code that stops using it:

- Dropping a column or table
- Renaming a column or table
- Adding a `NOT NULL` constraint with no default
- Narrowing a type

### Removing a column takes two releases

**Release 1 (expand).** Stop reading and writing the column in code. Leave it in the
database. Deploy. Both versions are now fine: the old one still reads it, the new one
ignores it.

**Release 2 (contract).** Now that no running version touches it, drop it:

```python
def upgrade() -> None:
    op.drop_column("submissions", "legacy_field")
```

Renaming is the same shape: add the new column, write to both, backfill, switch reads,
then drop the old one in a later release.

### Long migrations

A replica stays unhealthy while it migrates, and Watchtower may give up waiting. Apply a
long migration by hand before the release lands:

```bash
docker compose -f docker-compose.prod.yml --profile migrate run --rm migrate
```

By the time the new image arrives, the schema is already at head and each replica's boot
migration is a no-op.

---

### Deferred contraction (do this in a later release)

`students.github_username` and `subjects.github_repo` are no longer used by the code
(since 2026-09-17, migration 0027) but were left in place so the previous release could
keep serving during the rolling deploy. Once every replica runs a build newer than
2dc6708, add a migration with:

```python
op.drop_column("students", "github_username")
op.drop_column("subjects", "github_repo")
```

(`IF EXISTS` semantics are not needed — no release since has re-created them.)

## Creating the first account

A production deployment seeds no accounts — the demo accounts exist only when
`ENVIRONMENT=development`, which production never is. So a freshly deployed system
has an empty `users` table and no way in through the interface. Create the first
account directly in the database.

### 1. Generate a bcrypt hash

The application verifies with `bcrypt.checkpw`, so the hash must be bcrypt. Postgres's
`crypt()` and any SHA variant will not work.

Run this anywhere with Python — your laptop is fine, it never touches the database:

```bash
python3 -c "import bcrypt; print(bcrypt.hashpw(b'YOUR-PASSWORD-HERE', bcrypt.gensalt(12)).decode())"
```

Or on the host, without installing anything:

```bash
docker compose -f docker-compose.prod.yml exec app \
  python -c "import bcrypt; print(bcrypt.hashpw(b'YOUR-PASSWORD-HERE', bcrypt.gensalt(12)).decode())"
```

It prints something beginning `$2b$12$`. That whole string is the value to insert.

### 2. Insert a teacher

Teacher is the right first account: it can reach the portal, create subjects and
import students. Roles are `TEACHER`, `STUDENT` and `ADMIN`.

```sql
INSERT INTO users (username, password_hash, role, is_active)
VALUES ('yourname', '$2b$12$...paste the hash...', 'TEACHER', true);
```

Sign in at `https://<your domain>/auth/login`. A correct password answers `303` and
redirects to `/teacher`.

### Connecting a database client (DataGrip, psql, …)

Postgres is published on the host's **loopback only**:

```yaml
ports:
  - "127.0.0.1:${POSTGRES_HOST_PORT:-5432}:5432"
```

So it is reachable through an SSH tunnel and not reachable from the internet. The
`127.0.0.1` prefix is doing all the work and must not be removed: a plain
`- "5432:5432"` binds `0.0.0.0`, and Docker inserts its own iptables rules *ahead of*
the INPUT chain, so such a binding bypasses the host firewall completely. The Oracle
security list would not save you either. Verified: with the loopback binding the port
answers on `127.0.0.1` and is refused on the host's own external address.

**DataGrip.** New Data Source → PostgreSQL:

*SSH/SSL tab* → check **Use SSH tunnel** → configure the SSH host:

| Field | Value |
|---|---|
| Host | your server's public address |
| Port | `22` |
| User name | the SSH user (`ubuntu`, `opc`, `root` …) |
| Authentication | Key pair → your private key |

*General tab* — these are resolved **on the far side of the tunnel**, so they refer to
the server's own loopback:

| Field | Value |
|---|---|
| Host | `127.0.0.1` |
| Port | `5432` (or `POSTGRES_HOST_PORT`) |
| Database | `POSTGRES_DB` from `.env` |
| User | `POSTGRES_USER` from `.env` |
| Password | `POSTGRES_PASSWORD` from `.env` |

**Command line**, same idea:

```bash
ssh -L 5433:127.0.0.1:5432 user@your-host    # leave running
psql -h 127.0.0.1 -p 5433 -U "$POSTGRES_USER" -d "$POSTGRES_DB"
```

**Or skip the tunnel** for a quick look, with no client at all:

```bash
docker compose -f docker-compose.prod.yml exec postgres \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
```

### A student account needs a student row

`ck_users_student_role_has_student_id` requires any `STUDENT` user to reference a
`students` row, which itself requires a group. Creating students by hand is fiddly and
unnecessary: import them from CSV in the teacher portal, which creates both records and
emails each student their credentials. Only use SQL for the first teacher.

---

## Rollback

```bash
# 1. Stop Watchtower, or it will immediately re-pull the faulty :main.
docker compose -f docker-compose.prod.yml stop watchtower

# 2. Pin the last good immutable tag (find it in the workflow run, or in GHCR).
sed -i 's/^APP_IMAGE_TAG=.*/APP_IMAGE_TAG=sha-abc1234/' .env

# 3. Recreate the app replicas on that build.
docker compose -f docker-compose.prod.yml up -d --force-recreate app

# 4. Verify, then fix forward on main. Once :main is good again:
#    reset APP_IMAGE_TAG=main and restart watchtower.
```

**Rolling the image back does not roll the schema back.** This is safe only because of
the migration rule above: the previous release is schema-compatible with the migrated
database by construction. If you ever break that rule, rollback stops being safe, which
is the real reason the rule exists.

---

## Backups and restore

The `backup` service runs on an interval (`BACKUP_INTERVAL_SECONDS`, default daily) and
captures both stores in the same run:

- `pg_dump --format=custom` → `$BACKUP_DIR/postgres/<timestamp>.dump`
- `mc mirror` of the MinIO bucket → `$BACKUP_DIR/minio/<bucket>/`

Both matter. Restoring only the database leaves snapshot rows pointing at objects that
no longer exist.

Dumps older than `BACKUP_RETENTION_DAYS` are pruned — but **only after a successful
run**, so a failing backup cannot also delete the last good one.

Point `BACKUP_DIR` at a **second block volume**. A backup on the same disk as the data
protects against corruption, not against losing the disk.

### Restore

```bash
# Database
docker compose -f docker-compose.prod.yml stop app
docker compose -f docker-compose.prod.yml exec -T postgres \
  pg_restore --clean --if-exists -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  < /mnt/backups/postgres/<timestamp>.dump
docker compose -f docker-compose.prod.yml start app

# Object storage
docker run --rm -v /mnt/backups:/backups --network submissions-checker_data quay.io/minio/mc sh -c '
  mc alias set restore http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" &&
  mc mirror --overwrite /backups/minio/<bucket> restore/<bucket>'
```

Verify a restore against a scratch database at least once. A backup that has never been
restored is a backup you do not know you have.

---

## Reading Watchtower's logs

A healthy idle cycle looks like this, once a minute:

```
Session done  Failed=0 Scanned=2 Updated=0
```

`Scanned=2` is the two app replicas and nothing else — if Postgres, MinIO or
Caddy ever appear in the count, label scoping has broken and the updater is one
bad release away from restarting your database.

`Updated=0` means the registry digest for `:main` has not moved. That is the
normal state between deploys. It is also what you see if CI cancelled a run: a
newer commit supersedes an in-flight build under the workflow's concurrency
group, so only the latest commit publishes.

### "Failed to retrieve container image info: No such image: sha256:…"

Watchtower is failing to *describe* a container, not to update one — note
`Failed=0` on the same cycle. Some container references an image ID that no
longer exists on disk. Find it:

```bash
docker inspect --format '{{.Name}} {{.State.Status}} {{.Image}}' $(docker ps -aq) | grep <the-sha>
```

**If it is `backup`, and it is `running`, fix it — this one is not cosmetic.**
Rebuilding `submissions-checker-backup:local` retags the name to a new image and
leaves the old ID dangling; a later prune deletes it while the container is
still running from it. The container survives only until it next stops. After a
reboot Docker cannot resolve the image, the container does not come back, and
backups stop with no error anywhere you would look.

```bash
docker compose -f docker-compose.prod.yml up -d --force-recreate backup
```

**Always recreate `backup` after rebuilding it**, for the same reason:

```bash
docker compose -f docker-compose.prod.yml build backup
docker compose -f docker-compose.prod.yml up -d --force-recreate backup
```

If it is some other, stopped container, it is genuinely cosmetic — remove it:

```bash
docker rm <that-container>     # or: docker container prune
```

---

## Memory budget

2GB total. These are ceilings, not reservations — steady-state usage is well under them,
but the ceilings must still fit or an OOM kill is one burst away.

| Component | Limit | Measured idle |
|---|---|---|
| OS + dockerd | ~180M | — |
| caddy | 48M | 15M |
| postgres (`shared_buffers=128MB`) | 384M | 29M |
| minio | 192M | 64M |
| app × 2 replicas | 640M | 95M each |
| watchtower | 48M | ~15M |
| backup | 48M | idle between runs |
| **Committed (limits)** | **~1540M** | **~495M actual** |
| **Free for a check sandbox** | **~500M** | comfortably more in practice |

Measured figures are from the full stack under light load. The limits are deliberately
well above them: they exist to contain a leak or a runaway, not to describe normal use.
PostgreSQL's `shared_buffers` is allocated lazily, so its resident size climbs under real
load — do not size its limit from the idle number.

`SANDBOX_MAX_MEMORY` (default `384m`) is what a single check may take out of that
headroom. A subject requesting more in its `config.yml` is clamped to it, and the clamp
is logged with the subject and both values.

**Redo this arithmetic whenever you add a service or raise a limit.**

Two facts make 2GB workable, and both are worth knowing before changing anything:

- Checks are effectively serialized. They are dispatched by the outbox processor, which
  holds a PostgreSQL advisory lock, so only one replica drains the queue and it does so
  sequentially. In practice one sandbox runs at a time.
- `max_connections=30` is the ceiling the replicas' pools are sized against
  (`2 × (DB_POOL_SIZE + DB_MAX_OVERFLOW)` = 16, leaving room for migrations and
  backups). It is not a number to raise when connections run out — lower the pools.

---

## Operations

```bash
# Logs
docker compose -f docker-compose.prod.yml logs -f app
docker compose -f docker-compose.prod.yml logs -f watchtower   # what it updated, when

# Actual memory use against the budget
docker stats --no-stream

# Force a deploy now instead of waiting for the poll
docker compose -f docker-compose.prod.yml restart watchtower

# Database shell
docker compose -f docker-compose.prod.yml exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"

# Pause automatic deploys (e.g. during an exam)
docker compose -f docker-compose.prod.yml stop watchtower
```

Service health, usage and alerting live in Grafana Cloud — see `docs/observability.md`.

### Watchtower and the Docker API version

Watchtower's last upstream release (1.7.1, 2023) negotiates Docker API 1.25. Docker
Engine 28 and newer require 1.44 and reject the connection:

```
client version 1.25 is too old. Minimum supported API version is 1.44
```

The compose file sets `DOCKER_API_VERSION=1.44` on the Watchtower service, which is what
makes it work against a current daemon. If a future daemon drops 1.44, raise that value.
If Watchtower breaks in a way the pin cannot fix, `nicholas-fedor/watchtower` is an
actively maintained drop-in fork.

Symptom to watch for: the deploy silently stops happening. Check with
`docker compose -f docker-compose.prod.yml logs watchtower` — it should log
`Only checking containers using enable label` on each pass.

### Updating a data service

Watchtower is label-scoped: only app replicas carry
`com.centurylinklabs.watchtower.enable=true`. PostgreSQL, MinIO and Caddy are never
updated automatically — an unattended database restart under live traffic is not a risk
worth taking for a patch release. Update them deliberately:

```bash
docker compose -f docker-compose.prod.yml pull postgres
docker compose -f docker-compose.prod.yml up -d postgres   # brief outage; plan it
```

---

## Known exposure and accepted risk

**The app container mounts the Docker socket.** It needs it to launch check sandboxes.
Socket access is root-equivalent on the host: anything that can write to it can start a
privileged container. The app runs as an unprivileged user inside its container and gets
socket access through a supplementary group, which limits the blast radius of a
container-local bug but does not change the fundamental exposure.

**Therefore this host should run nothing else of value.** No other application, no other
data. Treat it as dedicated.

**Proctoring evidence is private and must stay that way.** Snapshots are stored without
a public ACL, MinIO is not published or proxied, and frames are served only through
`/teacher/proctoring/snapshots/<id>`, which checks subject authorization before
returning a single byte. If you ever expose MinIO directly or restore a public-read ACL,
every student's webcam image becomes readable by anyone holding the object key.

### Login throttling and the origin check

- The login / forgot-password limiter (`LOGIN_MAX_ATTEMPTS`, `LOGIN_WINDOW_SECONDS`) is
  **per application process**. With two replicas an attacker gets at most twice the
  configured budget; that is accepted rather than adding Redis. The client address is the
  first `X-Forwarded-For` hop, which Caddy sets — do not put another proxy in front that
  rewrites it without forwarding the original.
- Cross-site `POST`s are refused by comparing `Origin` with `Host` (and `Sec-Fetch-Site`
  when present). Caddy passes both headers through untouched; a proxy that rewrote `Host`
  to an internal name would make every browser form submission fail with 403.
