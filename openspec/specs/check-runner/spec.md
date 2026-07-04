# check-runner

## Purpose

A DB-free check-core and a standalone, versioned runner CLI that let teachers evaluate
submissions against a subject's real Docker sandbox without any app service. The check-core is
the single check code path shared with production, and the runner image plus suite-file schema
form the semver-stable contract that subject repos depend on.

## Requirements

### Requirement: DB-free check-core shared with production

The system SHALL expose a pure check-core function that, given a parsed subject config, an assignment code, an optional variant, and a submission directory, resolves the effective sandbox settings (merging the assignment `common.sandbox` block with the selected variant override), runs the configured `validate_command` and `check_command` through the existing Docker sandbox, parses each `/output/result.json`, recomputes the score by summing per-test `points_earned`/`max_points`, and applies `min_pass_score` to decide pass/fail. The check-core MUST NOT depend on the database, outbox, state machine, AI, or object storage. The production `execute_check_task` MUST be refactored to call this same check-core and then persist its result, so there is a single check code path.

#### Scenario: Core resolves common and variant sandbox settings

- **WHEN** the check-core is invoked for an assignment whose variant overrides only `check_command` while the `common.sandbox` block defines `image`, `tool`, `memory`, `cpus`, `timeout_seconds`, and `min_pass_score`
- **THEN** the effective settings used for the run SHALL take `image`/`tool`/`memory`/`cpus`/`timeout_seconds`/`min_pass_score` from the common block and `check_command` from the variant

#### Scenario: Core recomputes score and applies threshold

- **WHEN** the check script writes a `result.json` whose per-test `points_earned`/`max_points` sum to a percentage at or above `min_pass_score`
- **THEN** the returned outcome SHALL report `passed = true` with the recomputed score, ignoring any top-level `passed`/`score` the script may have written

#### Scenario: Validation failure short-circuits checks

- **WHEN** the configured `validate_command` exits non-zero for a submission
- **THEN** the check-core SHALL report the run as failed with the validation reason and SHALL NOT run the `check_command`

#### Scenario: Production and runner share one path

- **WHEN** the same submission and config are evaluated by `execute_check_task` and by the standalone runner
- **THEN** both SHALL produce the same pass/fail and score because both call the same check-core

### Requirement: Standalone runner CLI

The system SHALL provide a standalone command-line runner that loads a subject `config.yml` using the same parsing rules as the platform plugin loader and evaluates checks through the check-core against the real Docker sandbox. It SHALL support a single-check command (`run`) taking config path, assignment, optional variant, and submission directory, and a suite command (`run-suite`) taking a suite file path. The runner MUST NOT require a database, scheduler, or any app service to run. It SHALL emit human-readable output by default and machine-readable output when `--json` is given, and SHALL exit non-zero if any evaluated case does not match its expectation.

#### Scenario: Single check against a fixture

- **WHEN** a teacher runs `runner run --config config.yml --assignment lab1 --variant 3 --submission ./fixtures/correct/`
- **THEN** the runner SHALL execute the real sandbox for that assignment/variant and print the resulting pass/fail and score, exiting zero on a successful run

#### Scenario: Suite drives the subject's real image

- **WHEN** the runner evaluates a suite whose cases reference the subject's baked sandbox image
- **THEN** each case SHALL run inside that real image via the host Docker socket, with the production sandbox isolation (read-only filesystem, no network, memory/cpu limits, writable `/tmp` and `/output`)

#### Scenario: Mismatch fails the run

- **WHEN** any suite case produces an outcome that differs from its declared `expect` (or `expect_score` when given)
- **THEN** the runner SHALL report the mismatch and exit non-zero

### Requirement: Declarative suite file

The system SHALL define a declarative suite file format that lists check cases, each specifying an assignment code, an optional variant, a submission fixture directory, an expected result (`pass` or `fail`), and an optional expected score. The runner SHALL evaluate every case in the suite and assert each outcome against its expectation.

#### Scenario: Suite case asserts pass with expected score

- **WHEN** a suite case declares `expect: pass` and `expect_score: 100` for a correct fixture
- **THEN** the runner SHALL pass that case only if the real check yields a passing result whose score equals the expected score

#### Scenario: Suite case asserts failure for a wrong fixture

- **WHEN** a suite case declares `expect: fail` for a deliberately incorrect fixture
- **THEN** the runner SHALL pass that case only if the real check yields a failing result

### Requirement: Versioned runner image as the stability contract

The system SHALL package the runner CLI as a Docker image published from the app repository, tagged with an explicit version. The runner CLI flags and the suite file schema SHALL be the documented, semver-stable contract that subject repos depend on. Changes to app internals that do not change the runner CLI or suite schema MUST NOT require any change to subject repos.

#### Scenario: Teacher uses the image without a Python environment

- **WHEN** a teacher with only Docker installed pulls the published runner image and runs a suite via `docker run`
- **THEN** the suite SHALL execute with no separate Python environment or app service required

#### Scenario: App internal change does not break subjects

- **WHEN** the app adds a new internal dependency (for example redis) without changing the runner CLI flags or the suite schema
- **THEN** existing subject repos SHALL continue to pass `make test` against their pinned runner tag without modification

#### Scenario: CI builds and tags the image

- **WHEN** the runner image build workflow runs in the app repository
- **THEN** it SHALL build the runner image and tag it with its version so it can be pulled by subject repos

### Requirement: Production sandbox mounts resolve to host-visible paths under DinD

When the production check task (`execute_check_task`) runs inside a container that reaches the
Docker daemon via a bind-mounted Docker socket (Docker-in-Docker), every bind-mount source path
passed to the sandbox container SHALL be a path the host daemon can resolve, not a path that is
only meaningful inside the app container. This applies independently of how the plugin directory
at that path was populated (config-apply ZIP extraction).

#### Scenario: Plugin directory mount resolves on the host

- **WHEN** the check task executes a submission for a subject whose plugin config was extracted to
  a `plugins/<subjectCode>` directory by the config-apply service, and the app container talks to
  the host's Docker daemon
- **THEN** the `docker run -v` invocation for the plugin mount SHALL use the absolute host-side
  path to that plugin directory, not the relative or container-only path used elsewhere in-app

#### Scenario: Submission and output temp directories resolve on the host

- **WHEN** the check task extracts a submission ZIP and the sandbox writes its result to a
  temporary output directory
- **THEN** both temporary directories SHALL be created under a path that is bind-mounted
  identically on the host and inside the app container, so the host daemon can mount them into the
  sandbox container

### Requirement: Extracted submission tree is readable by the non-root sandbox user

When the production check task extracts a submitted ZIP archive before running it through the
sandbox, every directory and file in the extracted tree SHALL be traversable and readable by the
non-root user the sandbox container runs as, regardless of the permission bits stored in the
original archive entries.

#### Scenario: Submission file is readable inside the sandbox

- **WHEN** a student's submitted ZIP is extracted and bind-mounted read-only at `/submission`
  inside the sandbox container, and the sandbox image runs the check as a non-root user
- **THEN** that user SHALL be able to stat and read every extracted file without a
  `PermissionError`, regardless of the mode bits the ZIP entries originally carried

#### Scenario: Nested submission directories are traversable

- **WHEN** the submission ZIP contains files inside nested subdirectories
- **THEN** the non-root sandbox user SHALL be able to traverse every intermediate directory to
  reach those files

### Requirement: A crashed check script fails the submission visibly instead of wedging it

When the production check task's call into the check-core raises (a non-zero check-script exit,
or a missing/invalid `result.json`), the system SHALL transition the submission to a failed
state with the error recorded, rather than leaving it stuck in an intermediate status with no
valid outbound transition. The failure SHALL be visible to the student and teacher through the
normal submission-status UI, matching the standalone runner CLI's existing graceful handling of
the same error.

#### Scenario: Check script crash fails the submission
- **WHEN** a check script exits non-zero or writes an invalid `result.json` during production
  check execution
- **THEN** the submission transitions to a failed status with a recorded error message, and does
  not remain stuck in `VALIDATING`

#### Scenario: Failed submission is retryable via normal resubmission
- **WHEN** a submission has failed due to a check-script crash
- **THEN** the student can see the failure and resubmit through the normal submission flow,
  without any stuck-state requiring manual DB intervention

### Requirement: Sandbox timeout terminates the actual container

When a sandboxed check run exceeds its configured timeout, the system SHALL terminate the
running sandbox container itself, not only the local process that launched it, so a hung or
slow student script cannot continue consuming CPU/memory in an orphaned container past the
configured timeout.

#### Scenario: Timed-out container is killed
- **WHEN** a sandbox run exceeds its configured timeout
- **THEN** the system issues a kill against the specific running container (identified by a
  unique name assigned at launch), in addition to terminating the local wrapper process

#### Scenario: Container already exited is handled gracefully
- **WHEN** the timeout fires but the container has already exited on its own right before the
  kill is issued
- **THEN** the kill attempt's failure is swallowed and does not surface as an error

### Requirement: Sandbox output reading is bounded by file count

The sandbox result-reading step SHALL cap the number of output files it reads into memory, in
addition to the existing per-file size cap, so a check script that writes an excessive number of
files to `/output` cannot force unbounded memory use.

#### Scenario: Excess output files are not read
- **WHEN** a check script writes more files to `/output` than the configured file-count cap
- **THEN** only files up to the cap are read into memory; files beyond the cap are omitted from
  the result without raising an error
