## ADDED Requirements

### Requirement: Compiled-language checker support

The shared checker library SHALL support compiled languages via a compile-then-run flow. It SHALL compile the student source once (e.g. C++ via `g++ -std=c++17 -O2 -pthread`), and if compilation fails it SHALL fail all of the assignment's test cases with a message containing the compiler error rather than crashing. When compilation succeeds it SHALL run the produced binary per declarative `Case`, feeding stdin and applying the same precise matchers used for interpreted languages. The required submission filename SHALL be configurable per subject (e.g. `solution.cpp`).

#### Scenario: Compile failure fails tests with the compiler error

- **WHEN** a submitted `solution.cpp` does not compile
- **THEN** every test case is reported failed with 0 points, the message includes the `g++` error output, and the check script still exits 0 with a valid `result.json`

#### Scenario: Successful compile runs the binary per case

- **WHEN** `solution.cpp` compiles and a `Case` declares stdin and an expected numeric result
- **THEN** the compiled binary is executed once per case with that stdin, and the matcher is applied to its output exactly as for an interpreted submission

#### Scenario: Multithreaded C++ runs deterministically

- **WHEN** an adapted multithreading lab compiles a `std::thread` program with `-pthread` and joins all threads before printing final results
- **THEN** the checker feeds stdin, runs the binary, and asserts the deterministic final output

### Requirement: Config-defined quizzes per assignment

A subject SHALL be able to attach a quiz to an assignment by declaring a `quiz` block in `config.yml`, with no engine code change. The quiz SHALL be read from the pinned plugin config at runtime. Each question SHALL carry a `type` (single or multiple choice), `text`, `choices` (each with `is_correct`), and `points`. The block SHALL support at least `pass_threshold_pct`, `max_quiz_attempts`, `shuffle_questions`, `shuffle_options`, `questions_to_send`, and `show_correct_answers_after`. When `review_mode` is `tests_then_quiz`, the assignment's automated tests SHALL gate first and the quiz SHALL follow.

#### Scenario: Quiz is served from config

- **WHEN** an assignment declares a `quiz` block with questions and a student reaches the quiz step
- **THEN** the platform draws questions from the pinned plugin config (honoring `questions_to_send`/shuffle) and scores the attempt against `pass_threshold_pct` using each question's `points`

#### Scenario: Tests gate before the quiz

- **WHEN** `review_mode` is `tests_then_quiz` and a submission's automated tests have not passed
- **THEN** the student is not advanced to the quiz until the tests pass

### Requirement: Topic-preserving task adaptation when originals are unrunnable

When a subject's source coursework cannot be compiled or run in the sandbox (e.g. Windows-only GUI/socket/IPC programs on a Linux checker), the subject SHALL provide an adapted, deterministically checkable console task that teaches the same topic, and SHALL document the adaptation alongside the assignment (e.g. a `TASK.md`) while retaining the original document as reference.

#### Scenario: Windows-only lab is adapted to a runnable console task

- **WHEN** a lab's original task is a Win32 GUI/socket/IPC program that cannot run on the checker
- **THEN** the assignment ships a `TASK.md` describing a console C++ task on the same topic (e.g. multithreading → `std::thread` parallel compute) that compiles and runs deterministically, and an auto-checker grades it stdin→stdout

#### Scenario: Original document retained for reference

- **WHEN** a lab's task has been adapted
- **THEN** the original lab PDF remains in the assignment directory as reference material
