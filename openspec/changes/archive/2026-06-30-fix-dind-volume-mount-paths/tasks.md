## 1. Settings

- [x] 1.1 Add `host_plugins_dir: str | None = None` to `Settings` in `src/submissions_checker/core/config.py`, next to `plugins_dir`, with a comment explaining it's the host-absolute path used only for DinD sandbox bind mounts.

## 2. Sandbox mount path

- [x] 2.1 In `src/submissions_checker/workers/tasks/check_tasks.py`, change the `plugin_dir` construction (around line 91-92) to use `settings.host_plugins_dir or settings.plugins_dir` as the root, instead of `settings.plugins_dir` alone.

## 3. Compose configuration

- [x] 3.1 In `docker-compose.yml`'s `app` service `environment` block, add `HOST_PLUGINS_DIR=${PWD}/plugins`.
- [x] 3.2 In `docker-compose.yml`'s `app` service `volumes` block, add `- /tmp:/tmp`.

## 4. Verification (user-performed)

- [x] 4.1 Ask the user to run `docker compose up -d app` to pick up the new env var/volume.
- [x] 4.2 Ask the user to resubmit work as the test student for the previously-failing `pythonBasics` assignment and report whether the submission now reaches `TESTING`/graded instead of failing with the Docker daemon error.
