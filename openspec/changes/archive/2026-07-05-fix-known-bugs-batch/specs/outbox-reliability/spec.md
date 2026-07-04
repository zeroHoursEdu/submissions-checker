## ADDED Requirements

### Requirement: Retired outbox event types fail fast instead of retrying to a silent dead end

When the outbox processor encounters a message whose event type is a retired/no-longer-dispatched
type, the system SHALL mark it failed immediately with a clear log entry, rather than retrying it
up to the configured retry limit against an unknown-type error before going silent.

#### Scenario: Retired event type is dropped without retry
- **WHEN** a stray outbox row exists with a retired event type that no code path dispatches
- **THEN** the message is marked failed on its first processing attempt, with a log entry naming
  the retired event type, and is not retried

#### Scenario: Genuinely unknown event types still retry
- **WHEN** an outbox row has an event type that is neither a currently-dispatched type nor a
  known retired type
- **THEN** the existing retry-then-fail behavior is unchanged
