## Why

Every check run through the normal `docker-compose.yml` app service fails with
`docker: Error response from daemon: ... includes invalid characters for a local volume name`.
The app container talks to the **host's** Docker daemon via the bind-mounted
`/var/run/docker.sock` (a DinD sibling-container pattern), but `check_tasks.py` builds the
sandbox's plugin-mount source from the relative `plugins_dir` setting (`"plugins"`), and the
host daemon cannot resolve a relative bind-mount source. This same "DinD same-path mount" problem
was already identified and fixed for the standalone runner CLI and worked around in
`docker-compose.e2e.yml`, but that fix was never ported to the main compose file used for normal
teacher/student testing — so every real submission check is currently broken.

## What Changes

- Add a new `host_plugins_dir` setting, distinct from the existing `plugins_dir`, holding the
  absolute **host**-side path to the plugins directory for use only when constructing the
  Docker-in-Docker bind-mount source. `plugins_dir` keeps its current meaning (container-side
  path used by the in-container `PluginLoader` startup scan) and is unaffected.
- `check_tasks.py`'s sandbox `plugin_dir` construction uses `host_plugins_dir` when set, falling
  back to `plugins_dir` (today's behavior) when unset, so non-DinD/test environments are
  unaffected.
- `docker-compose.yml`'s `app` service sets `HOST_PLUGINS_DIR=${PWD}/plugins` and adds a
  `/tmp:/tmp` same-path volume mount so the submission-extraction and sandbox-output temp
  directories (also built from container-relative `tempfile` paths) are host-visible to the
  daemon during checks.

## Capabilities

### Modified Capabilities
- `check-runner`: production check execution (`execute_check_task`, the shared check-core's
  Docker sandbox invocation) must resolve every Docker-in-Docker bind-mount source to a
  host-visible path when running inside the app container, not only when running through the
  standalone runner CLI.

## Impact

- `src/submissions_checker/core/config.py` — new `host_plugins_dir` setting.
- `src/submissions_checker/workers/tasks/check_tasks.py` — sandbox `plugin_dir` construction.
- `docker-compose.yml` — `app` service env var + volume mount.
- No database schema or API changes. No change to the standalone runner CLI or
  `docker-compose.e2e.yml` (already has its own working, if differently-shaped, fix).
