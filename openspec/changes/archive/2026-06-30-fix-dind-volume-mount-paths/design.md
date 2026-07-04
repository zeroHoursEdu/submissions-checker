## Context

The app container runs sandboxed checks by shelling out to the Docker CLI against the **host's**
Docker daemon (`/var/run/docker.sock` is bind-mounted in, `docker-compose.yml:66`) — a
Docker-in-Docker sibling-container pattern. Any `-v host:container` bind-mount source the app
process passes must be a path the host daemon can resolve, not a path meaningful only inside the
app container.

`settings.plugins_dir` (`core/config.py:79`, default `"plugins"`) is currently read by two
different consumers with two different path requirements:
1. `main.py`'s startup `PluginLoader.load_all(Path(settings.plugins_dir), ...)` — runs natively
   inside the app container, scanning the filesystem for `config.yml`s. Needs the
   **container-side** path (`/app/plugins`, via `WORKDIR=/app` + the existing
   `./plugins:/app/plugins` mount). This already works correctly today.
2. `check_tasks.py:92`'s sandbox `plugin_dir`, passed to `DockerSandbox` and embedded in a
   `docker run -v ...` call resolved by the **host** daemon. Needs the **host-side** path. This is
   currently broken — it reuses the same relative `plugins_dir` value, producing an invalid
   relative bind-mount source.

`docker-compose.e2e.yml` already solves consumer 2 for its own stack by overriding the single
`PLUGINS_DIR` env var to a hardcoded absolute host path, and by mounting `- /tmp:/tmp` (a
same-path mount that also fixes the `tempfile`-based `student_files_dir`/`output_dir` mounts).
That override is never read by `main.py`'s startup loader correctly inside that same container
(the absolute host path doesn't exist there either), so the e2e stack's startup plugin scan is
silently broken — it works around it by seeding plugin data through other means. The same
shortcut would break startup plugin loading if copied into the main compose file.

## Goals / Non-Goals

**Goals:**
- Production check execution (the same path the standalone runner already gets right) succeeds
  when run through `docker-compose.yml`'s `app` service.
- The two path-purpose consumers stay independently correct: in-container scanning keeps using a
  container-relative path; DinD bind-mounts get a host-absolute path.
- No regression to the startup `PluginLoader` scan.

**Non-Goals:**
- Reconciling `docker-compose.e2e.yml`'s existing (different-shaped, already-working for its own
  purposes) approach — out of scope for this change.
- Changing the standalone runner CLI, which already handles this correctly via its own
  same-path-mount convention.
- General-purpose abstraction over "host path of a container path" — a single new setting,
  scoped to this one mount, is sufficient.

## Decisions

**New `host_plugins_dir` setting, not a `plugins_dir` override.** Keeps the two consumers
decoupled: `plugins_dir` (container-relative, used by the startup scan) is untouched;
`host_plugins_dir` (host-absolute, used only by the DinD bind-mount source) is new and optional,
falling back to `plugins_dir` when unset so test/CI environments that don't set it keep today's
behavior (relative path — already broken there today if they exercise the real sandbox, but no
worse than before).

**`HOST_PLUGINS_DIR=${PWD}/plugins` via Compose variable interpolation, not a hardcoded path.**
`docker-compose.e2e.yml` hardcodes a specific developer's absolute path
(`/home/vampir/petProjects/submissions-checker/plugins`), which only works on that one machine.
`${PWD}` is substituted by Compose from the invoking shell at `docker compose up` time, giving the
correct absolute host path on any machine, as long as Compose is invoked from the repo root (the
same assumption the existing `build: context: .` and `./plugins:/app/plugins` mount already make).

**`/tmp:/tmp` same-path mount for the two `tempfile`-based mounts, no code change.** Both
`student_files_dir` (extracted submission ZIP) and `output_dir` (sandbox output) are
`tempfile.TemporaryDirectory()`s that default to `/tmp/...`. Mounting host `/tmp` at the
container's `/tmp` makes that path identical on both sides — the existing, proven pattern from
`docker-compose.e2e.yml` — so no path-translation setting is needed for these two.

## Risks / Trade-offs

- [Risk] `${PWD}` is empty/wrong if Compose is invoked from a different directory or by tooling
  that doesn't export `PWD`. → Mitigation: this matches the existing implicit assumption of the
  `build.context: .` and `./plugins:/app/plugins` lines already in the file; no new fragility
  class introduced.
- [Risk] Mounting host `/tmp` into the container at `/tmp` exposes the container to (and from) all
  other host temp files, not just this app's. → Mitigation: same trade-off already accepted in
  `docker-compose.e2e.yml`; scoping to a dedicated subdirectory was considered but rejected to
  keep behavior identical to the already-verified e2e pattern.
- [Risk] `host_plugins_dir` unset in an environment that does exercise the real sandbox (e.g. a
  future CI job) silently reproduces today's bug. → Mitigation: falling back to `plugins_dir`
  preserves current (broken-for-DinD, fine-for-non-DinD) behavior rather than failing differently;
  acceptable since this change only targets the reported `docker-compose.yml` flow.

## Migration Plan

No data migration. Deploy: pull/restart the `app` service after the compose file and code changes
land so the new env var and volume mount take effect. No rollback complexity — reverting the
three changed files restores prior (broken) behavior.

## Open Questions

None — the user has deferred aligning `docker-compose.e2e.yml` to this same approach to a
separate, explicitly-approved follow-up.
