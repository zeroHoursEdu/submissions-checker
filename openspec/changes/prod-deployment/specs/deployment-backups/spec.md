## ADDED Requirements

### Requirement: The database is backed up on a schedule

The production stack SHALL produce a consistent logical backup of the PostgreSQL database on a recurring schedule without operator action. Backups SHALL be retained for a configurable number of days and older backups SHALL be pruned automatically so that the host's disk cannot fill.

#### Scenario: Backup is produced on schedule

- **WHEN** the configured interval elapses
- **THEN** a timestamped database dump SHALL be written to the backup destination

#### Scenario: Old backups are pruned

- **WHEN** a backup older than the configured retention window exists
- **THEN** it SHALL be removed during a subsequent backup run

#### Scenario: A failed backup is visible

- **WHEN** a backup run fails
- **THEN** the failure SHALL be recorded in the service's logs with a non-zero result, rather than exiting silently

### Requirement: Object storage contents are backed up

The contents of the MinIO bucket, which holds proctoring snapshots and assignment files, SHALL be mirrored to the backup destination on the same schedule as the database, so that a restore recovers both the records and the files they reference.

#### Scenario: Snapshots survive a volume loss

- **WHEN** the MinIO data volume is lost and the most recent mirror is restored
- **THEN** the proctoring snapshots referenced by rows in `quiz_attempt_snapshots` SHALL be retrievable again

### Requirement: Restoring from a backup is documented and exercised

The deployment documentation SHALL describe the restore procedure for both the database and object storage, in enough detail to follow on an unfamiliar host. The procedure SHALL be verified at least once against a real backup artifact before the change is considered complete.

#### Scenario: Operator restores onto an empty host

- **WHEN** an operator follows the documented procedure on a freshly provisioned host using a stored backup
- **THEN** the application SHALL start against the restored database and serve previously created submissions, attempts and snapshots

#### Scenario: Backup is proven restorable, not merely produced

- **WHEN** the change is implemented
- **THEN** a restore SHALL have been performed at least once from an actual backup artifact, confirming the dump is complete and loadable
