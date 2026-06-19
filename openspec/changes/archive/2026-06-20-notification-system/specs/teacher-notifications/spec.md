## ADDED Requirements

### Requirement: Teacher email address storage

The system SHALL store an optional email address for each `User` so teacher and admin accounts can receive notifications. The column SHALL be nullable; existing rows and accounts without an email SHALL remain valid.

#### Scenario: Teacher has an email

- **WHEN** a teacher account has a non-null `email`
- **THEN** the system MAY deliver notification emails to that address

#### Scenario: Teacher has no email

- **WHEN** a teacher account has a null `email`
- **THEN** the system SHALL skip email delivery for that teacher and log the skip without raising an error

### Requirement: Enqueue teacher review notification on review transition

When a submission transitions into `AWAITING_TEACHER_REVIEW`, the system SHALL enqueue exactly one pending teacher-notification entry per responsible teacher, within the same database transaction as the status change.

The responsible teacher SHALL be the owner of the submission's subject. If the subject has no owner, the system SHALL enqueue one entry per active admin user as a fallback.

Enqueueing SHALL be idempotent per (teacher, submission): re-entering the review state or re-processing the same event SHALL NOT create duplicate pending entries.

#### Scenario: Single submission enters teacher review

- **WHEN** a submission reaches `AWAITING_TEACHER_REVIEW` via the tests-then-teacher path
- **THEN** the system enqueues one pending entry addressed to the owning teacher of the subject

#### Scenario: Submission enters teacher review after AI review

- **WHEN** a submission reaches `AWAITING_TEACHER_REVIEW` via the ai-then-teacher path
- **THEN** the system enqueues one pending entry addressed to the owning teacher of the subject

#### Scenario: Subject has no owner

- **WHEN** a submission for an ownerless (startup-loaded) subject reaches `AWAITING_TEACHER_REVIEW`
- **THEN** the system enqueues one pending entry per active admin user

#### Scenario: Duplicate enqueue suppressed

- **WHEN** an enqueue is attempted for a (teacher, submission) pair that already has a pending entry
- **THEN** the system does not create a second entry

### Requirement: Coalesced digest delivery

The system SHALL periodically flush pending teacher-notification entries, grouping all of a teacher's unsent entries into a single digest email that lists every pending work (student name, assignment title, and a review link). After a successful send, the included entries SHALL be marked sent so they are never re-sent.

A teacher's pending batch SHALL be flushed when EITHER the configured coalescing window has elapsed since the teacher's oldest pending entry, OR the number of pending entries for that teacher reaches the configured eager threshold. Entries that do not yet meet either condition SHALL remain pending for a later flush.

The flusher SHALL be safe to schedule on multiple workers: only one flush SHALL run at a time (advisory lock).

#### Scenario: Whole group submits within the window

- **WHEN** many submissions for one teacher enter review within the coalescing window
- **THEN** the teacher receives a single digest email listing all of those works rather than one email per submission

#### Scenario: Eager flush on threshold

- **WHEN** a teacher's pending entries reach the eager count threshold before the window elapses
- **THEN** the system flushes immediately and sends one digest email for those entries

#### Scenario: Window elapses with a trickle

- **WHEN** the coalescing window elapses since a teacher's oldest pending entry and fewer than the threshold are pending
- **THEN** the system sends one digest email containing the currently-pending entries

#### Scenario: Entries are not double-sent

- **WHEN** a digest email is sent successfully
- **THEN** the included entries are marked sent and excluded from future flushes

#### Scenario: No channel or no email configured

- **WHEN** no notification channel is configured, or the target teacher has no email
- **THEN** the system logs and skips delivery without marking entries sent in error and without raising

### Requirement: Configurable digest behavior

The system SHALL expose configuration for: enabling/disabling teacher digests, the coalescing window length, the eager count threshold, and the flush poll interval. When teacher digests are disabled, no flush SHALL occur and no digest emails SHALL be sent.

#### Scenario: Digests disabled

- **WHEN** the teacher-digest feature is disabled in configuration
- **THEN** the flush job performs no sends and pending entries accumulate untouched
