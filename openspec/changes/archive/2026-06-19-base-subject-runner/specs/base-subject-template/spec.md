## ADDED Requirements

### Requirement: Forkable base subject repo

The system SHALL provide a base subject repository that a teacher can fork to author a new subject. The repo SHALL contain a working skeleton: a `config.yml` with at least one example assignment (a `common.sandbox` block plus one variant), a vendored `checklib/` with the shared public API (cases, runner, fixtures, matchers, yaml_runner, structured), an `assignments/example-lab1/` with an example check script and assignment placeholder, a per-subject `Dockerfile`, and a `CONTRACT.md` documenting the engine-to-checker interface. The example SHALL pass out of the box so a fresh fork is immediately verifiable.

#### Scenario: Fresh fork passes its own example

- **WHEN** a teacher forks the base repo and runs its test target without editing anything
- **THEN** the bundled example assignment SHALL pass, proving the toolchain is wired correctly

#### Scenario: checklib is vendored and baked into the image

- **WHEN** the subject image is built from the repo `Dockerfile`
- **THEN** `checklib` SHALL be installed into the image so check scripts can import it inside the sandbox, and `checklib` SHALL NOT be provided by the runner

### Requirement: Test harness wired to the runner image

The base repo SHALL provide a `Makefile` whose `make test` target runs the published runner image against the repo's `tests/suite.yml`, mounting the repo and the host Docker socket so checks run in the real sandbox. It SHALL also provide a `make test-unit` target that runs `checklib`/check logic natively for fast iteration without the runner image. The repo SHALL include `tests/suite.yml` and `tests/fixtures/correct/` and `tests/fixtures/wrong/` sample submissions for the example assignment. The repo MUST NOT copy any app infrastructure (docker-compose, postgres, scheduler, S3); its only external dependency SHALL be the runner image.

#### Scenario: make test runs the real sandbox via the runner

- **WHEN** a teacher runs `make test` in the forked repo
- **THEN** the runner image SHALL evaluate every case in `tests/suite.yml` against the subject's real sandbox image and report pass/fail, exiting non-zero on any mismatch

#### Scenario: make test-unit gives a fast local loop

- **WHEN** a teacher runs `make test-unit`
- **THEN** the check logic SHALL run natively without pulling or running the runner image, for quick feedback before the authoritative `make test`

#### Scenario: No app infrastructure is vendored

- **WHEN** the base repo is inspected
- **THEN** it SHALL NOT contain docker-compose files, database, scheduler, or storage configuration from the app, and its pinned runner tag SHALL be the sole coupling to the platform

### Requirement: Pinned runner tag with documented bump path

The base repo SHALL pin a specific runner image tag and document how a teacher bumps it. The teacher-facing `README` SHALL describe the workflow: fork, edit `config.yml`, add fixtures to `tests/`, run `make test`, then push.

#### Scenario: Runner tag is pinned and explained

- **WHEN** a teacher reads the repo documentation
- **THEN** it SHALL state which runner tag is pinned and how to change it to adopt a newer runner

### Requirement: CI validates teacher commits

The base repo SHALL include a CI workflow that, on push and pull request, builds the subject sandbox image and then runs `make test`, so that a teacher's committed config and checks are validated automatically.

#### Scenario: CI builds the image then runs the suite

- **WHEN** a teacher pushes a commit or opens a pull request in a forked repo
- **THEN** CI SHALL build the subject image and run the suite, failing the build if any case does not match its expectation
