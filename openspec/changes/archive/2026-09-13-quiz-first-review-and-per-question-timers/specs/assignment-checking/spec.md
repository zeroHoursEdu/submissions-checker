## ADDED Requirements

### Requirement: Check-free review modes for quiz-examined assignments

The system SHALL support two review modes that accept a student's uploaded work without running
any automated check: `quiz_only` and `quiz_then_teacher`. For an assignment in either mode the
system SHALL validate only that the uploaded archive is a well-formed, safe ZIP, SHALL NOT start a
sandbox container, and SHALL NOT require a `sandbox` or `check_command` entry in the subject
config. The submission SHALL then advance directly to the quiz step. Under `quiz_only` a passed
quiz SHALL complete the submission; under `quiz_then_teacher` a passed quiz SHALL route the
submission to teacher review of the attached work, and the teacher's approval SHALL complete it.

#### Scenario: Assignment with no checker reaches the quiz

- **WHEN** a student submits to an assignment whose `review_mode` is `quiz_only` or `quiz_then_teacher` and whose config declares no `check_command`
- **THEN** no sandbox container is started and the submission advances to the quiz step instead of failing validation

#### Scenario: Unsafe or corrupt archive is still rejected

- **WHEN** a submission in a check-free review mode contains a corrupt ZIP or one with unsafe entry paths
- **THEN** the submission fails validation with a teacher-facing reason and no quiz is offered

#### Scenario: Passing the quiz under quiz_then_teacher

- **WHEN** a student passes the quiz for an assignment whose `review_mode` is `quiz_then_teacher`
- **THEN** the submission moves to teacher review, the teacher is notified, and the attached work is available to them

#### Scenario: Teacher downloads the submitted archive

- **WHEN** a teacher opens the review page for a submission in a check-free review mode
- **THEN** the student's uploaded archive can be downloaded from that page, and the download is refused to anyone who is not the subject's owner or an administrator

#### Scenario: Teacher approves after the quiz

- **WHEN** a teacher approves a submission that has already passed its quiz
- **THEN** the submission completes with the grade derived from the quiz score, and the student is not sent back into the quiz

#### Scenario: Passing the quiz under quiz_only

- **WHEN** a student passes the quiz for an assignment whose `review_mode` is `quiz_only`
- **THEN** the submission completes immediately with the grade derived from the quiz score

#### Scenario: Grade comes from the quiz alone

- **WHEN** a submission in a check-free review mode is graded and the assignment weights the quiz at 100%
- **THEN** the final grade is the quiz percentage scaled into the assignment's grade band, with no test component

## MODIFIED Requirements

### Requirement: Config-defined quizzes per assignment

A subject SHALL be able to attach a quiz to an assignment by declaring a `quiz` block in `config.yml`, with no engine code change. The quiz SHALL be read from the pinned plugin config at runtime. Each question SHALL carry a `type` (single or multiple choice), `text`, `choices` (each with `is_correct`), and `points`. The block SHALL support at least `pass_threshold_pct`, `max_quiz_attempts`, `shuffle_questions`, `shuffle_options`, `questions_to_send`, `show_correct_answers_after`, `time_limit_minutes`, `question_time_default_seconds`, and a per-question `time_limit_seconds`. When `review_mode` is `tests_then_quiz`, the assignment's automated tests SHALL gate first and the quiz SHALL follow; when `review_mode` is `quiz_only` or `quiz_then_teacher`, no tests run and the quiz SHALL be the gate.

#### Scenario: Quiz is served from config

- **WHEN** an assignment declares a `quiz` block with questions and a student reaches the quiz step
- **THEN** the platform draws questions from the pinned plugin config (honoring `questions_to_send`/shuffle) and scores the attempt against `pass_threshold_pct` using each question's `points`

#### Scenario: Tests gate before the quiz

- **WHEN** `review_mode` is `tests_then_quiz` and a submission's automated tests have not passed
- **THEN** the student is not advanced to the quiz until the tests pass

#### Scenario: Quiz is the only gate

- **WHEN** `review_mode` is `quiz_only` or `quiz_then_teacher`
- **THEN** the student reaches the quiz as soon as their upload is accepted, with no test gate in front of it

### Requirement: Topic-preserving task adaptation when originals are unrunnable

When a subject's source coursework cannot be compiled or run in the sandbox (e.g. Windows-only GUI/socket/IPC programs on a Linux checker), the subject SHALL either (a) provide an adapted, deterministically checkable console task that teaches the same topic, or (b) keep the original task and examine it by quiz using a check-free review mode. In both cases the subject SHALL document what the student must submit alongside the assignment (e.g. a `TASK.md`) and SHALL retain the original document as reference.

#### Scenario: Windows-only lab is adapted to a runnable console task

- **WHEN** a lab's original task is a Win32 GUI/socket/IPC program that cannot run on the checker and the subject chooses to auto-check it
- **THEN** the assignment ships a `TASK.md` describing a console C++ task on the same topic (e.g. multithreading → `std::thread` parallel compute) that compiles and runs deterministically, and an auto-checker grades it stdin→stdout

#### Scenario: Windows-only lab is kept and examined by quiz

- **WHEN** a lab's original task is a Win32 GUI/socket/IPC program and the subject chooses to examine it rather than adapt it
- **THEN** the assignment keeps the original task, uses `quiz_only` or `quiz_then_teacher`, and ships a `TASK.md` stating what the student uploads and that the quiz is the defence

#### Scenario: Original document retained for reference

- **WHEN** a lab's task has been adapted or examined by quiz
- **THEN** the original lab PDF remains in the assignment directory as reference material
