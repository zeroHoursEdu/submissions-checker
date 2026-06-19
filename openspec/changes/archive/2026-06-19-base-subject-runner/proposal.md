## Why

Subjects are standalone teacher-owned repos (`config.yml` + vendored `checklib/` + `assignments/labN/` + per-subject `Dockerfile` + `CONTRACT.md`) that teachers fork, fill in, and commit. But there is **no way to run the real production check pipeline against a fixture submission outside the full app**. Each subject's `tests/test_e2e.py` runs check scripts via raw subprocess — it never exercises the real Docker sandbox (read-only fs, `--network none`, memory/cpu limits, `/tmp` tmpfs), the real baked subject image, or the real `config.yml` wiring (common/variant resolution, `validate_command`, `min_pass_score`, score recompute). So a teacher's "passing" local test can still fail in production. Booting the full app to test a config is a non-starter: it drags in postgres, localstack/S3, the scheduler, AI, and email — none of which a teacher should ever copy.

The fix is to **invert the dependency**. The only coupling surface between the app and a subject repo becomes (a) the sandbox `CONTRACT.md` interface and (b) a versioned **runner artifact**. App internals (postgres, redis, scheduler, AI, S3) live behind that surface. When the app later adds redis, the runner image is rebuilt but its CLI contract is unchanged — subject repos do not change; they bump the pinned runner tag only when they choose.

## What Changes

- **Extract a pure, DB-free check-core** from `workers/tasks/check_tasks.py`: `run_check(config, assignment_code, variant, submission_dir) -> CheckOutcome` that resolves the common/variant sandbox block, runs `validate_command` then `check_command` via the existing `DockerSandbox`, parses `/output/result.json`, recomputes score from per-test `points_earned/max_points`, and applies `min_pass_score` — with **no** database, outbox, state machine, AI, or S3.
- **Refactor `execute_check_task`** to call the new core and then persist, so production and the runner share **one** code path (no drift). *(behavior-preserving refactor)*
- **New standalone CLI runner** exposing the core: `runner run --config ... --assignment ... --variant ... --submission ...` (single check) and `runner run-suite tests/suite.yml` (declarative suite of `{assignment, variant, submission_fixture_dir, expect: pass|fail, expect_score?}`). Loads the subject config the same way `PluginLoader` does, runs each case against the **real** Docker sandbox driving the subject's own baked image via the host `docker.sock`, asserts outcomes, non-zero exit on mismatch, human + `--json` output.
- **Publish the runner as a versioned Docker image** `submissions-checker-runner:<version>` from the app repo (primary distribution: `docker pull` + `make test`, no Python env needed), built/tagged by a CI workflow. The runner CLI + `suite.yml` schema are documented as a **semver stability contract** — the one thing subject repos depend on.
- **New forkable base subject repo template** at `/home/vampir/petProjects/baseSubjectRepo`: skeleton `config.yml`, vendored `checklib/`, an `assignments/example-lab1/`, per-subject `Dockerfile`, `CONTRACT.md`, `tests/suite.yml` + `tests/fixtures/{correct,wrong}/`, a `Makefile` (`make test` → runner image; `make test-unit` → fast native loop), a teacher-facing `README`, and a `.github/workflows/check.yml` that builds the subject image then runs `make test` on push/PR.

Non-goals: retrofitting the existing `pythonBasicSubject`/`cppBasicSubject` to `suite.yml` (follow-up; design stays compatible). `checklib` remains vendored per subject and baked into the subject image — it is **not** part of the runner.

## Capabilities

### New Capabilities
- `check-runner`: A DB-free check-core shared with production plus a standalone, versioned CLI runner (shipped as a Docker image) that validates a subject config against the real sandbox via a declarative suite file.
- `base-subject-template`: A forkable base subject repo that gives teachers a working config + checklib + example assignment + fixtures + `make test` wired to the runner image + CI, with zero app-infra copy.

### Modified Capabilities
<!-- The check_tasks refactor preserves existing assignment-checking requirements (behavior-identical), so no delta spec is required. -->

## Impact

- **Code (app repo):** `workers/tasks/check_tasks.py` (split into check-core + persist), `services/docker_sandbox.py` (reused unchanged), new `services/check_core.py` (or similar), new CLI module + entrypoint, new `services/plugin_loader.py` config-parse reuse. New runner `Dockerfile` + CI workflow.
- **New repo:** `/home/vampir/petProjects/baseSubjectRepo` (template, separate git repo).
- **Dependencies:** runner image needs Docker socket access (DinD) at run time; no new app runtime deps. Teachers need only Docker + `make`.
- **Contract surface:** runner CLI flags + `suite.yml` schema + sandbox `CONTRACT.md` become the versioned, semver-stable boundary between app and subjects.
- **Compatibility:** existing subjects keep working unchanged; `suite.yml` adoption is opt-in.
