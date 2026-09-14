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

### Requirement: Every published build is recorded as a GitHub Release

Each successful publication from the default branch SHALL create a GitHub Release carrying an immutable tag, so that the history of what was shipped is visible without reading workflow logs, and so an operator choosing a rollback target can see what each build contained.

The release SHALL state the image reference an operator would pin to roll back to it, and SHALL list the changes it contains relative to the previous release.

#### Scenario: Publishing creates a release

- **WHEN** a push to the default branch passes every gate and the image is published
- **THEN** a GitHub Release SHALL exist for that commit, tagged immutably, naming the image tag and digest that were published

#### Scenario: Release names its rollback reference

- **WHEN** an operator opens a release to roll back to it
- **THEN** the release body SHALL contain the exact immutable image tag to set as the deployed tag

#### Scenario: Pull requests create no release

- **WHEN** the workflow runs for a pull request
- **THEN** no release and no tag SHALL be created

### Requirement: The running application reports the build it came from

The application SHALL expose the source revision of the image it is running on an unauthenticated endpoint. Without this, a pull-based deployment gives no way to tell whether a published build has actually reached production.

The revision SHALL be fixed at image build time and SHALL NOT be configurable at run time, so it cannot disagree with the code in the image.

#### Scenario: Endpoint reports the built revision

- **WHEN** the version endpoint is requested from a running container
- **THEN** it SHALL return the commit the image was built from

#### Scenario: Locally built image without a revision

- **WHEN** the image was built without a revision supplied
- **THEN** the endpoint SHALL report an explicit unknown value rather than failing

### Requirement: Deployment of a published build is tracked to its conclusion

Because deployment is initiated by the host rather than by CI, publication alone SHALL NOT be reported as deployment. The pipeline SHALL record a GitHub Deployment for each published build and SHALL resolve it by observing the production host, marking it successful only once the host is serving the published revision.

A build that is published but never picked up by the host SHALL surface as a failed deployment rather than as a success, since a silently stalled updater is a known failure mode of pull-based delivery.

#### Scenario: Host picks up the build

- **WHEN** the production host replaces its replicas with the published build
- **THEN** the deployment SHALL be marked successful once the host reports the published revision

#### Scenario: Host never picks up the build

- **WHEN** the host does not report the published revision within the configured window
- **THEN** the deployment SHALL be marked failed, and the failure SHALL be attributable to the deployment rather than to the build

#### Scenario: No production host configured

- **WHEN** no production host has been configured for the repository
- **THEN** the workflow SHALL skip deployment tracking and SHALL still publish and release normally
