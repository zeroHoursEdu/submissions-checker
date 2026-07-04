## ADDED Requirements

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
