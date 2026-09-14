## ADDED Requirements

### Requirement: Pushes to the default branch publish an application image

The system SHALL provide a continuous delivery workflow that runs on every push to `main` and, on success, publishes the application image to the GitHub Container Registry. The workflow SHALL also run on pull requests targeting `main`, building the image without publishing it, so that build breakage is caught before merge.

#### Scenario: Merge to main publishes

- **WHEN** a commit lands on `main`
- **THEN** the workflow SHALL build and push the application image, and the published image SHALL contain that commit's code

#### Scenario: Pull request builds but does not publish

- **WHEN** the workflow runs for a pull request
- **THEN** the image SHALL be built and validated, and nothing SHALL be pushed to the registry

### Requirement: Quality gates precede publication

The workflow SHALL run the project's linting, formatting, type-checking and test suites before building the publishable image. A failure in any gate SHALL fail the workflow and SHALL prevent publication, so a broken build can never become the image the production host pulls.

#### Scenario: Failing tests block the release

- **WHEN** the test suite fails on a push to `main`
- **THEN** no image SHALL be pushed and the previously published image SHALL remain the one production runs

#### Scenario: Type errors block the release

- **WHEN** type-checking reports an error
- **THEN** the workflow SHALL fail before the build step

### Requirement: Published images are multi-architecture

The application image SHALL be published as a manifest covering both `linux/amd64` and `linux/arm64`. The production host runs 64-bit ARM, while contributor machines are predominantly x86-64, and both SHALL be able to pull the same tag.

#### Scenario: ARM host pulls the production image

- **WHEN** the ARM production host pulls the published tag
- **THEN** it SHALL receive the `linux/arm64` variant and run it without emulation

#### Scenario: x86 developer pulls the same tag

- **WHEN** a contributor on x86-64 pulls the same tag
- **THEN** they SHALL receive the `linux/amd64` variant

### Requirement: Each build is addressable by both a moving and an immutable tag

Every published build SHALL be tagged with a moving tag tracking the default branch and with an immutable tag identifying the originating commit. The moving tag is what the production updater follows; the immutable tag is what an operator pins to when rolling back.

#### Scenario: Moving tag advances with main

- **WHEN** a new commit is published
- **THEN** the moving branch tag SHALL now resolve to the new build's digest

#### Scenario: Commit tag never changes

- **WHEN** a commit's image has been published
- **THEN** its commit-identified tag SHALL continue to resolve to that same digest for later builds of other commits

### Requirement: The running deployment discovers new images by polling the registry

The production host SHALL learn about new builds by polling the registry for a change in the moving tag's digest. The pipeline SHALL NOT require inbound network access to the production host, and no host credentials such as SSH keys SHALL be stored in the CI system.

#### Scenario: Host behind a firewall still updates

- **WHEN** the production host permits no inbound connections from the internet
- **THEN** it SHALL still pick up and deploy new builds, because the update is initiated from the host

#### Scenario: Private registry package is pullable from the host

- **WHEN** the published package is private
- **THEN** the host SHALL authenticate to the registry with a read-scoped credential and SHALL pull the image successfully

#### Scenario: No deploy credentials in CI

- **WHEN** the workflow's secrets are inspected
- **THEN** they SHALL contain no credential granting shell or administrative access to the production host

### Requirement: The production image is built for production

The published image SHALL be built from a production target that installs runtime dependencies only, excludes development and test dependencies, does not install the project in editable mode, and includes the Docker client needed to launch check sandboxes. Its health check MUST NOT depend on tools installed solely for that purpose.

#### Scenario: Development dependencies are absent

- **WHEN** the published image is inspected for installed packages
- **THEN** test and lint tooling SHALL NOT be present

#### Scenario: Container reports its own health

- **WHEN** the container health check runs
- **THEN** it SHALL query the application's health endpoint using an interpreter already present in the image, and SHALL report unhealthy while the application is still starting
