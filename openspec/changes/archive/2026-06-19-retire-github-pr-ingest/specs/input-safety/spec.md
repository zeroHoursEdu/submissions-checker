## REMOVED Requirements

### Requirement: Restricted git transport for clones
**Reason**: The only code path that clones repositories was the GitHub PR ingest
(`pull_tasks` → `utils/git.clone_repository`), which is being deleted. With no clone
path remaining, there is no attacker-controlled clone URL to restrict.
**Migration**: None. Submissions are uploaded as ZIP archives, which remain covered by
the "Safe archive extraction" requirement. If repository cloning is reintroduced later,
this requirement must be restored.

Repository clones SHALL be restricted to safe transports and validated URLs so
attacker-controlled clone URLs cannot execute commands.

#### Scenario: ext transport blocked
- **WHEN** a clone is attempted with an `ext::`/`fd::` or non-GitHub URL
- **THEN** the clone is refused
