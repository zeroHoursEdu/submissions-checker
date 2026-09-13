## ADDED Requirements

### Requirement: Time penalties apply to the current question in stepper mode

When an attempt is delivered one question at a time, a `reduce_time` anti-cheat action SHALL
subtract its `penalty_seconds` from the remaining time of the question currently being answered,
rather than from a whole-attempt budget. If the penalty exhausts that question's remaining time,
the question SHALL expire immediately and be recorded as timed out with zero points. When the
attempt is delivered as a single page, `reduce_time` SHALL continue to subtract from the
attempt-wide `time_limit_minutes` budget exactly as it does today.

#### Scenario: Penalty shortens the current question

- **WHEN** a `reduce_time` rule fires with `penalty_seconds: 10` while a student is on a question with 25 seconds left
- **THEN** the countdown drops to 15 seconds and the rest of the attempt is unaffected

#### Scenario: Penalty exhausts the current question

- **WHEN** a `reduce_time` rule fires with a penalty larger than the current question's remaining time
- **THEN** the question expires at once, is recorded as timed out with zero points, and the next question is served

#### Scenario: Single-page attempt is unaffected

- **WHEN** a `reduce_time` rule fires during an attempt that has no per-question limits
- **THEN** the penalty is deducted from the attempt's `time_limit_minutes` budget, identical to current behavior
