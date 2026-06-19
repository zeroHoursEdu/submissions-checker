## 1. Extract DB-free check-core (app repo)

- [x] 1.1 Add `services/check_core.py` with a `CheckOutcome` dataclass (`passed`, `score`, `max_score`, `tests`, `reason`) and `run_check(config, assignment_code, variant, submission_dir) -> CheckOutcome`.
- [x] 1.2 Move the sandbox-block resolution into the core: merge `assignments.<code>.common.sandbox` with the selected variant override (only `validate_command`/`check_command` vary per variant; `image`/`tool`/`memory`/`cpus`/`timeout_seconds`/`min_pass_score` come from common).
- [x] 1.3 In the core, run `validate_command` (if set) then `check_command`(s) via the existing `DockerSandbox.run`; short-circuit to a failed outcome with reason if validation exits non-zero.
- [x] 1.4 In the core, parse each `/output/result.json`, recompute score by summing per-test `points_earned`/`max_points`, ignore script-supplied top-level `passed`/`score`, and apply `min_pass_score`.
- [x] 1.5 Refactor `workers/tasks/check_tasks.py::execute_check_task` to call `run_check` then persist (`test_results`, state transitions, outbox); ensure no DB/outbox/AI/S3 logic leaks into the core.
- [x] 1.6 Factor the pure `config.yml` YAML→dict parsing out of `services/plugin_loader.py` into a shared helper the core/runner reuse; leave the loader's DB upsert in place.

## 2. Standalone runner CLI (app repo)

- [x] 2.1 Add a runner module (`cli/runner.py` or `runner/__main__.py`) with subcommands `run` and `run-suite` and a console entrypoint.
- [x] 2.2 Implement `run --config --assignment --variant --submission`: load config via the shared parser, call `run_check`, print outcome, exit non-zero on a failed run.
- [x] 2.3 Define and parse the `suite.yml` schema (list of `{assignment, variant?, submission, expect: pass|fail, expect_score?}`); validate required fields with clear errors.
- [x] 2.4 Implement `run-suite`: evaluate every case through `run_check`, assert outcome against `expect`/`expect_score`, aggregate results, exit non-zero on any mismatch.
- [x] 2.5 Add human-readable table output plus `--json` machine output.

## 3. Runner image + contract (app repo)

- [x] 3.1 Add `docker/runner/Dockerfile` packaging the runner CLI; configure it to drive the host Docker daemon via a mounted `/var/run/docker.sock`.
- [x] 3.2 Add a CI workflow that builds and version-tags `submissions-checker-runner:<version>`.
- [x] 3.3 Write `docs/runner-contract.md` documenting the runner CLI flags + `suite.yml` schema as the semver-stable contract; note app internals live behind it.

## 4. Tests for the engine side (app repo)

- [x] 4.1 Unit-test `run_check` sandbox-block resolution (common + variant merge) without a database.
- [x] 4.2 Unit-test score recompute + `min_pass_score` threshold and validation short-circuit.
- [x] 4.3 Add a parity test asserting `execute_check_task` and the runner produce identical pass/fail + score for the same fixture (single shared code path).
- [x] 4.4 Add a runner CLI test driving `run-suite` against a tiny fixture subject (real sandbox if Docker available; otherwise a marked/skipped integration test).

## 5. Base subject repo template (new repo)

- [x] 5.1 Initialize git repo at `/home/vampir/petProjects/baseSubjectRepo`.
- [x] 5.2 Add skeleton `config.yml`: subject metadata + one `example-lab1` assignment with a `common.sandbox` block and one variant.
- [x] 5.3 Vendor `checklib/` (cases, runner, fixtures, matchers, yaml_runner, structured) with the shared public API.
- [x] 5.4 Add `assignments/example-lab1/` with an example `check.py` and an assignment placeholder.
- [x] 5.5 Add a per-subject `Dockerfile` (`python:3.12-slim`, `pip install` checklib, drop to non-root uid 10001, baked image name e.g. `mysubject-checker:local`).
- [x] 5.6 Add `CONTRACT.md` (language-agnostic engine↔checker interface).
- [x] 5.7 Add `tests/suite.yml` plus `tests/fixtures/correct/` and `tests/fixtures/wrong/` sample submissions for the example assignment.

## 6. Template harness, docs, CI (new repo)

- [x] 6.1 Add `Makefile` `make test` target: `docker run --rm -v $(PWD):/subject -v /var/run/docker.sock:/var/run/docker.sock submissions-checker-runner:<pinned> run-suite /subject/tests/suite.yml` (also build the subject image first).
- [x] 6.2 Add `Makefile` `make test-unit` target running checklib/check logic natively for fast iteration.
- [x] 6.3 Add teacher-facing `README.md`: fork → edit `config.yml` → add fixtures → `make test` → push; document the pinned runner tag and how to bump it.
- [x] 6.4 Add `.github/workflows/check.yml`: on push/PR build the subject image then run `make test`.

## 7. Verification

- [x] 7.1 Build the runner image locally and run `run-suite` against the template's `tests/suite.yml`; confirm the bundled example passes out of the box.
- [x] 7.2 Run app-side tests (`make test`/`make lint`/`make type-check`) and confirm the check_tasks refactor is green with no behavior change.
- [x] 7.3 Confirm the template repo contains no app infra (no docker-compose, postgres, scheduler, S3) and that its only platform coupling is the pinned runner tag.
