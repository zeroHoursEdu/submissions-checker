## ADDED Requirements

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
