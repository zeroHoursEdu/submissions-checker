# input-safety

## ADDED Requirements

### Requirement: Safe archive extraction
Uploaded ZIP archives SHALL be extracted only after validating every entry
against path traversal and resource-exhaustion limits.

#### Scenario: Zip Slip entry rejected
- **WHEN** an uploaded ZIP contains an entry resolving outside the destination
  directory (e.g. `../../etc/x`, absolute path, or symlink)
- **THEN** extraction aborts with an error and writes nothing outside the destination

#### Scenario: Zip bomb rejected
- **WHEN** an uploaded ZIP's total uncompressed size or entry count exceeds the
  configured limit
- **THEN** extraction aborts with an error

### Requirement: Restricted git transport for clones
Repository clones SHALL be restricted to safe transports and validated URLs so
attacker-controlled clone URLs cannot execute commands.

#### Scenario: ext transport blocked
- **WHEN** a clone is attempted with an `ext::`/`fd::` or non-GitHub URL
- **THEN** the clone is refused
