## Why

After fixing the DinD bind-mount path bug (`fix-dind-volume-mount-paths`), every real check now
runs the sandbox container, exposing a second latent bug: the check fails with
`PermissionError: [Errno 13] Permission denied: '/submission/solution.py'` raised from the
plugin's `validate.py`. `check_tasks.py` extracts the submitted ZIP into a
`tempfile.TemporaryDirectory()`, whose default mode is `0700` (owner/traversable by the app
container's user only), then bind-mounts it read-only into the sandbox at `/submission`. The
sandbox image drops to a non-root uid before running checks (the exact same condition already
documented and fixed for the `/output` mount in `docker_sandbox.py`'s
`os.chmod(output_dir, 0o777)`), so the non-root check process can't even traverse into
`/submission` to stat a file. The equivalent fix was never applied to the submission-extraction
directory.

## What Changes

- After `safe_extract()` populates the submission extraction directory in `check_tasks.py`, widen
  its permissions (and its contents') so the non-root sandbox user can traverse and read every
  extracted file — mirroring the existing `output_dir` precedent in `docker_sandbox.py`.

## Capabilities

### Modified Capabilities
- `check-runner`: production check execution must make the extracted submission directory
  readable by the non-root sandbox user, not just the plugin and output directories.

## Impact

- `src/submissions_checker/workers/tasks/check_tasks.py` — permission widening after extraction.
- No database, API, or compose changes.
