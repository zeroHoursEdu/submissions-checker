## ADDED Requirements

### Requirement: Replicas are replaced one at a time

When a new image is published, the deployment SHALL replace application replicas sequentially, never stopping more than one replica at a time, and SHALL leave at least one replica serving traffic throughout. The update mechanism SHALL be scoped by container label so that only application replicas are eligible for automatic replacement.

#### Scenario: One replica restarts while the other serves

- **WHEN** a new image is published and the updater begins a rolling replacement of two replicas
- **THEN** at every point during the rollout at least one replica SHALL be running and reachable through the proxy

#### Scenario: Stateful services are never auto-updated

- **WHEN** a newer PostgreSQL, MinIO or Caddy image becomes available upstream
- **THEN** the updater SHALL NOT replace those containers, because they do not carry the update-enable label

#### Scenario: Rollout is not triggered by unrelated images

- **WHEN** the updater polls the registry and the application image digest is unchanged
- **THEN** no container SHALL be restarted

### Requirement: A replica drains in-flight requests before exiting

A replica being replaced SHALL stop accepting new connections, finish the requests already in progress, and only then exit. The container stop grace period SHALL be long enough to cover the slowest ordinary request, and MUST be longer than the platform default of ten seconds.

#### Scenario: Quiz submission in flight completes

- **WHEN** a student's `POST /quiz/{attempt_id}/submit` is being processed on a replica that receives a termination signal
- **THEN** that request SHALL complete and return its normal response, and the attempt SHALL be graded and persisted exactly once

#### Scenario: Slow request is not cut off at the default grace period

- **WHEN** a request taking longer than ten seconds is in flight as its replica is replaced
- **THEN** the replica SHALL remain alive until the request finishes or the configured grace period elapses, whichever comes first

### Requirement: Requests that reach a stopping replica are retried elsewhere

The proxy SHALL detect a replica that is no longer accepting connections and SHALL retry the request against another healthy replica. Retries SHALL be limited to failures that occur before the request reaches the application, so that non-idempotent requests are never executed twice.

#### Scenario: Connection refused is retried transparently

- **WHEN** a request is dispatched to a replica that has just closed its listening socket, and the connection is refused
- **THEN** the proxy SHALL retry the request on another healthy replica and the client SHALL receive a normal response rather than a 502

#### Scenario: An application error is not retried

- **WHEN** a replica accepts a request and returns a 500 response
- **THEN** the proxy SHALL return that response to the client and SHALL NOT re-send the request to another replica

#### Scenario: A quiz answer is recorded once

- **WHEN** a `POST /quiz/{attempt_id}/answer` is retried by the proxy after a refused connection
- **THEN** the answer SHALL be recorded exactly once, because the refused attempt never reached the application

### Requirement: Concurrent replica boots serialize their migrations

Database migrations run during application startup SHALL be guarded by a PostgreSQL advisory lock so that replicas booting concurrently cannot execute schema changes at the same time. A replica that cannot immediately acquire the lock SHALL wait for it rather than skipping migrations or failing to start.

#### Scenario: Two replicas boot together

- **WHEN** two replicas start simultaneously against a database with pending migrations
- **THEN** exactly one SHALL apply the migrations while the other waits, and both SHALL then serve traffic against the migrated schema

#### Scenario: Lock is released when migration fails

- **WHEN** a migration raises an error partway through
- **THEN** the advisory lock SHALL be released and the failure SHALL be reported, so a subsequent boot can retry rather than hanging forever

#### Scenario: Lock is released when a replica dies mid-migration

- **WHEN** a replica holding the migration lock is killed
- **THEN** the lock SHALL be released by the database when its session ends, and the next replica to boot SHALL be able to acquire it

### Requirement: Releases are schema-compatible across adjacent versions

A release SHALL be deployable while the previous version is still serving traffic. Migrations within a single release MUST be backward-compatible with the previous release's code: columns and tables may be added, but a column or table still read by the previous release MUST NOT be dropped or renamed in the same release. Destructive changes SHALL be split across two releases using an expand-then-contract sequence.

#### Scenario: Mixed versions serve the same schema

- **WHEN** one replica runs the new version and another still runs the previous version during a rollout
- **THEN** both SHALL operate correctly against the migrated schema, and neither SHALL raise errors about missing or renamed columns

#### Scenario: Removing a column takes two releases

- **WHEN** a column is to be removed
- **THEN** the first release SHALL stop reading and writing it while leaving it in place, and only a subsequent release SHALL drop it

#### Scenario: The rule is documented for contributors

- **WHEN** a contributor writes a migration
- **THEN** the deployment documentation SHALL state the expand/contract requirement and give the two-release removal sequence

### Requirement: A failed release can be rolled back to a known-good image

The deployment SHALL publish an immutable, commit-identified image tag alongside the moving tag it tracks, so an operator can pin the stack to a previously working build. The rollback procedure SHALL be documented.

#### Scenario: Operator pins a previous build

- **WHEN** a release is found to be faulty and the operator sets the image to the immutable tag of the prior commit
- **THEN** the stack SHALL start that build, and the updater SHALL NOT immediately replace it with the faulty moving tag

#### Scenario: Rollback across a schema change is called out

- **WHEN** the faulty release included a migration
- **THEN** the documented procedure SHALL state that rolling the image back does not roll the schema back, and that this is safe only because migrations are backward-compatible
