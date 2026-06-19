## Context

The app (`submissions-checker`) checks student submissions through a Docker sandbox. The production path is `services/docker_sandbox.py` (`DockerSandbox.run`) invoked by `workers/tasks/check_tasks.py` (`execute_check_task`), which is welded to the DB, transactional outbox, state machine, AI, and S3. Subjects are standalone teacher repos loaded by `services/plugin_loader.py` from a `config.yml` (subject metadata + `assignments.<code>.common.sandbox` + per-variant overrides + optional quiz). Each subject bakes its own image with a vendored `checklib/` so check scripts can `import checklib` inside the sandbox.

Today a teacher cannot run the real check pipeline outside the app. Subject `tests/test_e2e.py` files run check scripts via subprocess — they bypass the real image, the real sandbox isolation, and the real config resolution, so they silently drift from production. Booting the app to test a config requires postgres + localstack + scheduler + AI + email, which teachers must never copy.

The sandbox contract is already small and stable (`CONTRACT.md`): `docker run --rm --network none --read-only --tmpfs /tmp -v sub:/submission:ro -v plugin:/plugin:ro -v out:/output {image} {tool} /plugin/{check_command} /submission`, script writes `/output/result.json`. This change turns that contract plus a versioned runner artifact into the *only* coupling between app and subjects.

## Goals / Non-Goals

**Goals:**
- A pure, DB-free check-core that production and a standalone runner both call (one code path, no drift).
- A standalone CLI runner that validates a subject `config.yml` against the **real** Docker sandbox via a declarative `suite.yml`, shipped as a versioned Docker image.
- A forkable base subject repo template with `make test` (real runner) + `make test-unit` (fast native) + CI, copying zero app infra.
- A semver-stable contract surface (runner CLI flags + `suite.yml` schema + sandbox `CONTRACT.md`) so app-internal changes (e.g. adding redis) never touch subject repos.

**Non-Goals:**
- Retrofitting `pythonBasicSubject`/`cppBasicSubject` onto `suite.yml` (follow-up; design stays compatible).
- Changing the sandbox isolation model or the `result.json` schema.
- Folding `checklib` into the runner. `checklib` stays vendored per subject and baked into the subject image.
- A hosted/registry strategy for subject images (teachers keep owning that).

## Decisions

### Decision 1: Extract a pure check-core; refactor production to call it
Create `services/check_core.py` exposing `run_check(config: dict, assignment_code: str, variant: str | None, submission_dir: Path) -> CheckOutcome`. It owns exactly the logic that is independent of persistence: resolve the effective sandbox block (merge `common.sandbox` with the variant's override — only `validate_command`/`check_command` vary per variant; `image`/`tool`/`memory`/`cpus`/`timeout_seconds`/`min_pass_score` come from `common`), call `DockerSandbox.run` for `validate_command` (if set) then `check_command`(s), parse each `/output/result.json`, recompute score by summing per-test `points_earned`/`max_points`, apply `min_pass_score`. `CheckOutcome` is a plain dataclass (`passed`, `score`, `max_score`, `tests`, `reason`).

`execute_check_task` is refactored to: load submission/config from the DB, call `run_check`, then persist (`test_results`, state transitions, outbox). All DB/outbox/AI/S3 logic stays in the worker; none leaks into the core.

- **Why:** Single code path is the whole point — the runner is only trustworthy if it executes the *same* resolution + scoring as production. Sharing the core guarantees that by construction.
- **Alternatives considered:** (a) Reimplement resolution/scoring in the runner — rejected: guaranteed drift, exactly the bug we're fixing. (b) Have the runner spin up the full worker against a throwaway sqlite/postgres — rejected: drags in the infra we're trying to escape.

### Decision 2: Runner is a thin CLI over the core, distributed as a Docker image
New module `runner/` (or `cli/runner.py`) with two subcommands: `run` (single check) and `run-suite` (declarative file). It parses `config.yml` by reusing the plugin loader's parsing (factor the pure YAML→dict parsing out of `PluginLoader` so both share it; the loader's DB upsert stays put). It calls `run_check` for each case and asserts against the expectation. Output: human table by default, `--json` for machines, non-zero exit on any mismatch.

Distribution = a published image `submissions-checker-runner:<version>` (a dedicated `docker/runner/Dockerfile` in the app repo). The image runs checks by talking to the **host** Docker daemon via a mounted `docker.sock` (DinD-by-socket), so it drives the subject's own baked image with full production isolation.

- **Why image-primary:** teachers need only Docker — no Python env, no app checkout. One `docker pull`. The socket-mount lets the runner launch the subject's real sandbox image.
- **Alternatives considered:** (a) pip package only — rejected as primary: forces a Python env on teachers and CI. (Could add later as a convenience; not in scope.) (b) Runner runs checks *inside itself* rather than launching the subject image — rejected: it would not test the subject's real image/deps.

### Decision 3: Declarative `suite.yml` as the teacher-facing test surface
`tests/suite.yml` is a list of cases: `{assignment, variant?, submission: <fixture_dir>, expect: pass|fail, expect_score?: int}`. The runner walks every case, runs it through the core, and asserts. Fixtures live under `tests/fixtures/` (e.g. `correct/`, `wrong/`).

- **Why:** declarative cases are reviewable, diffable, and language-agnostic; they map 1:1 to "given this submission, the check should pass/fail with this score" — which is exactly what a teacher wants to lock down. Mirrors the existing `cases.yml` ergonomics teachers already know.
- **Alternatives considered:** imperative Python test files (current `test_e2e.py`) — kept only as the optional `make test-unit` fast path, not the authoritative surface.

### Decision 4: Semver-stable contract surface
The runner CLI flags + `suite.yml` schema + sandbox `CONTRACT.md` are documented as the versioned boundary. App internals live behind it. Subjects pin a runner tag and bump on their own schedule.

- **Why:** this is the mechanism that makes "app adds redis → subjects untouched" literally true. The coupling is a tagged artifact + a documented schema, not shared compose or vendored app code.
- **Trade-off:** we must treat CLI/schema changes as breaking and version them deliberately. Accepted — it is a small, well-defined surface.

### Decision 5: Base template is a separate repo at `/home/vampir/petProjects/baseSubjectRepo`
Sibling to the existing subject repos. Contains skeleton `config.yml`, vendored `checklib/`, `assignments/example-lab1/`, per-subject `Dockerfile`, `CONTRACT.md`, `tests/suite.yml` + fixtures, `Makefile` (`make test` → runner image with socket mount; `make test-unit` → native), teacher `README`, `.github/workflows/check.yml` (build subject image → `make test`). The bundled example passes out of the box.

- **Why:** matches the established subject-per-repo model; teachers fork a known-good starting point. Keeping it a separate repo (not inside the app repo) keeps the dependency inversion honest.

## Risks / Trade-offs

- **Docker socket mount is privileged** → The runner needs `/var/run/docker.sock`. Document it; the runner only launches the configured sandbox image with the same locked-down flags production uses (no network, read-only, pid/mem/cpu caps). Note the trust boundary in the README.
- **CI needs a working Docker daemon for `make test`** → GitHub-hosted runners provide one; document the requirement. `make test-unit` covers environments without Docker for fast iteration.
- **Refactor could change production behavior** → Mitigate by making the extraction behavior-preserving and covering it with a test that asserts the runner and `execute_check_task` produce identical outcomes for the same fixture (the shared-path scenario).
- **`checklib` is duplicated per subject (vendored)** → Already the accepted model; runner stays out of it. Packaging `checklib` as an installable shared lib remains a separate deferred TODO.
- **Two coupled artifacts (app repo + template repo) in one change** → Land the engine side (core + runner + image) first within the change's task order, then the template that consumes it, so the template can be validated against a real runner image.
