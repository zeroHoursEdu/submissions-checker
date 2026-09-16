# Application image, in two targets.
#
#   dev   — editable install with dev dependencies, for docker-compose.yml (bind-mounted
#           source + uvicorn --reload) and for the e2e stack.
#   prod  — runtime dependencies only, non-editable, non-root, no test or lint tooling.
#           This is the target published to GHCR and run on the production host.
#
# `prod` is the final stage, so a plain `docker build` produces the production image.
# Development compose files select the dev target explicitly.
#
#   docker build -f docker/app/Dockerfile -t submissions-checker:local .
#   docker build -f docker/app/Dockerfile --target dev -t submissions-checker:dev .

# The Docker CLI the app shells out to when launching check sandboxes. Copied from the
# official CLI image rather than installed via apt `docker.io`, which drags in the daemon
# and roughly 200MB the app never uses. The daemon is the host's, reached over the socket.
FROM docker:27-cli AS dockercli


# ---------------------------------------------------------------------------
# Shared base: interpreter, uv, and the Docker client.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker

RUN pip install --no-cache-dir uv

WORKDIR /app


# ---------------------------------------------------------------------------
# dev — editable install with dev extras; source arrives via bind mount.
# ---------------------------------------------------------------------------
FROM base AS dev

# curl is used by the development compose healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src

RUN uv pip install --system -e ".[dev]"

# Migrations live in alembic/versions; alembic.ini points script_location there.
COPY alembic ./alembic
COPY alembic.ini ./
COPY templates ./templates
COPY static ./static
COPY i18n ./i18n
# Proctoring models are served from /static, never a CDN (see scripts/fetch_vendor_assets.py).
COPY scripts/fetch_vendor_assets.py scripts/vendor_assets.sha256 ./scripts/
RUN python scripts/fetch_vendor_assets.py

EXPOSE 8000

CMD ["sh", "-c", "uvicorn submissions_checker.main:app --host 0.0.0.0 --port ${PORT:-8000}"]


# ---------------------------------------------------------------------------
# builder — resolve runtime dependencies into a standalone prefix.
# ---------------------------------------------------------------------------
FROM base AS builder

COPY pyproject.toml ./
COPY src ./src

# Runtime dependencies only: no pytest, no mypy, no ruff, no testcontainers. Installed
# into a prefix that the runtime stage copies wholesale, so build tooling never ships.
RUN uv pip install --python /usr/local/bin/python --prefix /install --no-cache .


# ---------------------------------------------------------------------------
# prod — runtime only, unprivileged.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS prod

# The commit this image was built from, surfaced at /version. Baked in rather than
# passed at run time so it always describes the code actually inside the image:
# the release workflow polls that endpoint to decide whether a published build
# reached the host, and a value the host could set would make that meaningless.
ARG APP_REVISION=unknown

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_REVISION=${APP_REVISION}

COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=builder /install /usr/local

WORKDIR /app

# Migrations live in alembic/versions; alembic.ini points script_location there.
COPY alembic ./alembic
COPY alembic.ini ./
COPY templates ./templates
COPY static ./static
COPY i18n ./i18n
COPY docker/app/healthcheck.py ./healthcheck.py
# Proctoring models are served from /static, never a CDN (see scripts/fetch_vendor_assets.py).
COPY scripts/fetch_vendor_assets.py scripts/vendor_assets.sha256 ./scripts/
RUN python scripts/fetch_vendor_assets.py

# Socket access is granted at runtime by supplementary group (`group_add` in compose),
# because the host's docker group id is host-specific and must not be baked in.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /app/uploads \
 && chown -R app:app /app

USER app

EXPOSE 8000

# Dependency-free health probe: no curl in the image, and nothing installed solely to
# support the health check. Exits non-zero on any non-2xx or connection failure.
# The start period covers migrations plus application boot, so a replica applying a
# migration is not declared unhealthy and killed while it is still legitimately working.
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]

CMD ["sh", "-c", "exec uvicorn submissions_checker.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
