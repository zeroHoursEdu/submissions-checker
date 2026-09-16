# outbox-reliability

## Purpose

Defines reliability guarantees for the transactional outbox message processor — specifically,
that dispatch failures fail fast and visibly for event types that can never succeed, instead of
silently retrying to exhaustion.

## Requirements

### Requirement: Retired outbox event types do not exist in the schema
When an outbox event type is retired, the system SHALL remove it from the `outbox_event_type`
enum in a migration that first deletes any stray rows carrying it, rather than keeping a
never-dispatched value that the processor has to special-case. Migration 0027 did this for the
GitHub-era `PULL`, `REVIEW` and `NOTIFY` types.

#### Scenario: Unknown event types still fail after retries
- **WHEN** an outbox row has an event type that no code path dispatches
- **THEN** the existing retry-then-fail behavior applies (the unknown-type branch)
