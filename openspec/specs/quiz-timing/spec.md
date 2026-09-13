# quiz-timing Specification

## Purpose
TBD - created by archiving change quiz-first-review-and-per-question-timers. Update Purpose after archive.
## Requirements
### Requirement: Per-question time limits are configurable

A quiz SHALL support a time limit on an individual question via `time_limit_seconds` on that
question, and a quiz-level fallback `question_time_default_seconds` applied to every question
that does not declare its own. A question with neither SHALL have no limit. The resolved
per-question limits SHALL be captured in the attempt's `questions_snapshot` when the attempt
starts, so that editing the subject config afterwards does not alter an attempt already in
progress.

#### Scenario: Question-level limit wins over the quiz default

- **WHEN** a quiz sets `question_time_default_seconds: 30` and one question sets `time_limit_seconds: 60`
- **THEN** that question is served with a 60-second window and every other question with a 30-second window

#### Scenario: Quiz with no timing keys

- **WHEN** a quiz declares neither `question_time_default_seconds` nor any `time_limit_seconds`
- **THEN** no question carries a limit and the attempt is not a timed-per-question attempt

#### Scenario: Config edited mid-attempt

- **WHEN** the subject config's per-question limits are changed after an attempt has started
- **THEN** the in-progress attempt continues to use the limits captured in its snapshot

### Requirement: Stepper delivery for per-question-timed quizzes

When at least one question drawn for an attempt carries a time limit, the attempt SHALL be
delivered one question at a time: the quiz page shows the current question only, its position in
the attempt, and a countdown of the time remaining for that question. The student SHALL NOT be
able to return to an already-answered or already-expired question. When no drawn question carries
a limit, the quiz SHALL be delivered as a single page containing all questions, exactly as before
this change.

#### Scenario: Timed quiz is stepped

- **WHEN** a student opens an attempt in which at least one drawn question has a time limit
- **THEN** the page shows a single question with its own countdown and a progress indicator, and submitting it advances to the next question

#### Scenario: Untimed quiz keeps the single-page form

- **WHEN** a student opens an attempt in which no drawn question has a time limit
- **THEN** all questions are rendered on one page and submitted together, unchanged from the existing behavior

#### Scenario: No going back

- **WHEN** a student attempts to answer a question they have already passed
- **THEN** the answer is rejected and the student is returned to the current question, whose recorded answer is unchanged

### Requirement: Question expiry is enforced by the server

The remaining time for a question SHALL be computed by the server from the moment that question
was first served, not from client-side state. When a question's window has elapsed, the system
SHALL record it as answered with zero points and flagged as timed out, and advance to the next
question. This SHALL happen regardless of whether the student's browser was open, so that closing
or reloading the page neither pauses nor resets the clock. An answer submitted after its
question's window has elapsed SHALL be scored zero and flagged as timed out.

#### Scenario: Student sits on a question until it expires

- **WHEN** a question's window elapses while the page is open
- **THEN** the question is recorded as timed out with zero points and the next question is served

#### Scenario: Student closes the browser and returns later

- **WHEN** a student closes the quiz and reopens it after several question windows would have elapsed
- **THEN** every question whose window elapsed while they were away is recorded as timed out with zero points, and they resume at the first question still within its window

#### Scenario: Answer arrives after the window closed

- **WHEN** a student submits an answer for a question whose window has already elapsed
- **THEN** the answer is recorded, scored zero, and flagged as timed out, even if the selected option was correct

#### Scenario: Last question expires

- **WHEN** the final question's window elapses
- **THEN** the attempt is graded and finalized without further student action

### Requirement: Timed-out questions are surfaced to the student

The quiz result page SHALL identify which questions were lost to the clock, both as a count and
per question, so a student can tell a wrong answer apart from an unanswered one. The countdown
SHALL change appearance as a question's remaining time runs low.

#### Scenario: Result page marks the timed-out questions

- **WHEN** a student views the result of an attempt in which two questions expired
- **THEN** the page states that two questions were left unanswered because time ran out, and each of those questions is individually marked as timed out

#### Scenario: Countdown warns before expiry

- **WHEN** a question's remaining time falls into its final seconds
- **THEN** the countdown switches to its warning appearance

