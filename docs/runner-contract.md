# Runner contract (the app ↔ subject stability surface)

`submissions-checker-runner` is the **only** thing a subject repo depends on. Everything
else in the platform (postgres, redis, the scheduler, AI review, S3) lives *behind* this
surface. App internals can change freely; as long as the runner CLI and the `suite.yml`
schema below do not change, **subject repos never need to change**.

This contract is **semver-versioned** via the image tag. Subjects pin a tag and bump on
their own schedule.

## Distribution

Published as a Docker image:

```
ghcr.io/<owner>/submissions-checker-runner:<version>     # exact version
ghcr.io/<owner>/submissions-checker-runner:<major>       # latest within a major
```

Teachers need only Docker — no Python environment, no app checkout. The runner launches the
subject's own baked sandbox image through the **host Docker daemon**, so a mounted
`/var/run/docker.sock` is required.

Because the runner starts the subject image as a **sibling** container (via the host
socket), every path it bind-mounts must exist on the **host**. Mount the repo at the *same
path* inside the runner and point the sandbox's scratch dir at that mount via `TMPDIR`:

```bash
mkdir -p "$PWD/.runner-tmp"
docker run --rm \
  -v "$PWD":"$PWD" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -w "$PWD" \
  -e TMPDIR="$PWD/.runner-tmp" \
  ghcr.io/<owner>/submissions-checker-runner:1 \
  run-suite "$PWD/tests/suite.yml"
```

The base subject repo's `Makefile` (`make test`) wraps this for you.

## CLI

```
submissions-checker-runner [--json] <command>
```

`--json` switches output to machine-readable JSON. Exit codes: `0` all cases matched,
`1` at least one mismatch, `2` usage/config error, `3` sandbox technical failure.

### `run` — single check

```
run --config <config.yml> --assignment <code> [--variant <id>] \
    --submission <dir> [--expect pass|fail] [--expect-score <int>]
```

Runs one assignment/variant against one submission directory and asserts the outcome.

### `run-suite` — declarative suite

```
run-suite <suite.yml>
```

Runs every case in the suite and exits non-zero on any mismatch.

## `suite.yml` schema

```yaml
# Optional. Repo root used to resolve `config` and each case `submission`.
# Defaults to the suite file's parent's parent (so tests/suite.yml → repo root).
root: "."
# Optional. Subject config path, relative to root. Default: config.yml
config: config.yml

cases:
  - assignment: lab1        # required — assignment code in config.yml
    variant: "1"            # optional — omit for variant-less assignments
    submission: tests/fixtures/correct/lab1_v1   # required — dir mounted at /submission
    expect: pass            # required — pass | fail
    expect_score: 100       # optional — exact recomputed score to require on pass
    name: "lab1 v1 correct" # optional — display label
  - assignment: lab1
    variant: "1"
    submission: tests/fixtures/wrong/lab1_v1
    expect: fail
```

- `expect: pass` requires the recomputed score to clear the assignment's `min_pass_score`.
- `expect: fail` requires the submission to NOT pass (low score or validation failure).
- A misconfigured assignment (`config_error`) always fails the case regardless of `expect`.

## What is covered by semver

**Stable (breaking changes bump the major):**
- the `run` / `run-suite` commands and their flags,
- the `suite.yml` keys above,
- the sandbox interface in each subject's `CONTRACT.md` (`/submission`, `/plugin`,
  `/output/result.json`, `VARIANT`).

**Not part of the contract (may change any release):**
- app-internal modules, database schema, queueing, the runner's own dependencies,
- human-readable (non-`--json`) output formatting.
