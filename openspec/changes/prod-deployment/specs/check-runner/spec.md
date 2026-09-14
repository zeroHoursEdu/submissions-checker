## ADDED Requirements

### Requirement: Sandbox resource requests are bounded by a host maximum

The system SHALL enforce configurable upper bounds on the sandbox resources a subject's `config.yml` may request. A subject declaring more memory or CPU than the host permits SHALL have its request clamped to the configured maximum rather than being honoured, and the clamping SHALL be logged so the discrepancy is visible to operators and subject authors. The bounds SHALL be configuration, not constants, so a small production host and a large development machine can differ.

#### Scenario: Excessive memory request is clamped

- **WHEN** a subject's sandbox configuration requests `memory: 2g` while the configured maximum is `512m`
- **THEN** the sandbox SHALL be launched with `512m` and a warning SHALL be logged naming the subject, the requested value and the applied value

#### Scenario: Request within the bound is honoured unchanged

- **WHEN** a subject requests a value at or below the configured maximum
- **THEN** the sandbox SHALL be launched with exactly the requested value and no warning SHALL be logged

#### Scenario: Default remains usable without configuration

- **WHEN** no maximum is configured
- **THEN** a documented default maximum SHALL apply, and existing subjects using the default sandbox settings SHALL run unchanged

#### Scenario: Bound applies to the standalone runner too

- **WHEN** the same subject configuration is evaluated by the standalone runner
- **THEN** the same clamping SHALL apply, because both paths share the check-core
