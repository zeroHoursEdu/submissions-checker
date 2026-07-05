## ADDED Requirements

### Requirement: No manual subject-creation form exists
The teacher dashboard SHALL NOT present a manual create-subject form or button. ZIP-based config
upload (`POST /teacher/subjects/apply-config`) SHALL be the only way to create or update a
subject.

#### Scenario: Dashboard has no dead create-subject link
- **WHEN** a teacher views the dashboard
- **THEN** the only subject-creation affordance is the Upload Config button; no link or form
  targets a separate manual-creation route
