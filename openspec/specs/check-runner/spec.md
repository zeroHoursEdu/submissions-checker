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
only meaningful inside the app container. This applies independently of whichever path the
in-container plugin-loading scan uses.

#### Scenario: Plugin directory mount resolves on the host

- **WHEN** the check task executes a submission for a subject whose plugin config was loaded from
  a `plugins/<subjectCode>` directory, and the app container talks to the host's Docker daemon
- **THEN** the `docker run -v` invocation for the plugin mount SHALL use the absolute host-side
  path to that plugin directory, not the relative or container-only path used for the in-container
  plugin-loader scan

#### Scenario: Submission and output temp directories resolve on the host

- **WHEN** the check task extracts a submission ZIP and the sandbox writes its result to a
  temporary output directory
- **THEN** both temporary directories SHALL be created under a path that is bind-mounted
  identically on the host and inside the app container, so the host daemon can mount them into the
  sandbox container
