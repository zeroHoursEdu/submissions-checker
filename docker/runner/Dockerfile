# submissions-checker-runner — the standalone check runner shipped to subject repos.
#
# Subject repos depend ONLY on this image (its CLI + the suite.yml schema are the semver
# stability contract). It runs checks by driving the HOST Docker daemon via a mounted
# socket, so it launches each subject's own baked sandbox image with the same isolation
# production uses — no postgres / scheduler / S3 / app service required.
#
# Build:  docker build -f docker/runner/Dockerfile -t submissions-checker-runner:<ver> .
# Use:    docker run --rm \
#           -v "$PWD":/subject \
#           -v /var/run/docker.sock:/var/run/docker.sock \
#           submissions-checker-runner:<ver> run-suite /subject/tests/suite.yml

# Docker CLI only (the daemon is the host's, reached over the mounted socket).
FROM docker:29-cli AS dockercli

FROM python:3.12-slim

COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app

# The runner imports only the light check-core path (check_core, docker_sandbox,
# subject_config, logging, cli) — install just those deps, then the package itself
# without pulling in the heavy app dependencies (fastapi/sqlalchemy/openai/...).
# pydantic/pydantic-settings are needed only because core.logging imports core.config at
# module load; the runner never instantiates Settings, so no app env vars are required.
RUN pip install --no-cache-dir \
    "pyyaml>=6.0" \
    "structlog>=24.4.0" \
    "pydantic>=2.10.0" \
    "pydantic-settings>=2.6.0"

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

ENTRYPOINT ["submissions-checker-runner"]
