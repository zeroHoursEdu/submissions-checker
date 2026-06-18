# assignment-checking

## Purpose

The contract and reusable machinery for auto-checking student submissions: how a subject is
packaged (its own repo + custom runtime image), the shared `checklib` library that removes
per-assignment boilerplate, the precise output matchers and file-fixture harness, the
language-agnostic data-driven runner, and the documented sandbox interface that any
subject/language plugin implements.

## Requirements

### Requirement: Subject-per-repo packaging with custom image

A subject SHALL be packaged as a self-contained git repository containing its `config.yml`, per-assignment check scripts, fixtures, assignment documents, the `checklib` source, and a `Dockerfile` that builds the subject's runtime image. The `config.yml` `image:` field SHALL reference that image. The repository SHALL be loadable by the engine without engine code changes (placed under `plugins/` via symlink or clone), and SHALL include documentation for building the image and running checks locally.

#### Scenario: Subject repo builds a runnable image

- **WHEN** a maintainer runs `docker build` against the subject repo's `Dockerfile`
- **THEN** an image is produced containing the interpreter, the subject's third-party dependencies (e.g. `pandas`, `openpyxl`), and `checklib` installed into site-packages

#### Scenario: Engine loads the standalone repo unchanged

- **WHEN** the subject repo is symlinked or cloned into the monorepo `plugins/` directory and the engine starts
- **THEN** the plugin loader upserts the subject and its assignments from the repo's `config.yml`, and checks run using the image named in `image:`

### Requirement: Shared checker library

The system SHALL provide a shared checker library `checklib`, baked into the subject's runtime image and importable as `import checklib`, that removes per-assignment boilerplate. The library SHALL expose: a declarative `Case` model, a sandboxed subprocess runner, output matchers, a file-fixture harness, structured-content comparison, and a single `result.json` emitter. Check scripts SHALL import it directly from site-packages without `sys.path` manipulation.

#### Scenario: Check script imports the library

- **WHEN** a check script at `/plugin/assignments/labN/check.py` runs `from checklib import run_cases, Case`
- **THEN** the import succeeds inside the sandbox (because `checklib` is installed in the image) and the script can define test cases declaratively without re-implementing subprocess, parsing, or JSON-emission logic

#### Scenario: Emitter produces the engine-expected schema

- **WHEN** the library writes `/output/result.json`
- **THEN** the JSON contains a `tests` array whose entries each have `name`, `passed`, `points_earned`, `max_points`, `message`, and `description`, matching the schema the engine reads in `check_tasks.py`, and the process exits with code 0

### Requirement: Declarative stdin/stdout test cases

The library SHALL let a checker declare a test case by its stdin lines, an expected result, and a point value, and SHALL run the student program once per case, capturing stdout, stderr, and exit code with a per-case timeout.

#### Scenario: Numeric stdin to stdout case passes

- **WHEN** a `Case` declares stdin `["10", "4"]`, an expected numeric value `14` with default tolerance, and 10 points, and the student program prints `Сума: 14`
- **THEN** the case is marked passed and awards 10 points

#### Scenario: Per-case timeout is reported, not crashed

- **WHEN** the student program hangs or waits for more input than provided
- **THEN** the case fails with a clear timeout message and 0 points, the remaining cases still run, and the check script still exits 0 with a valid `result.json`

### Requirement: Precise output matchers

The library SHALL provide matchers that are precise enough to avoid the false positives of keyword/loose-number heuristics. It SHALL include at minimum: numeric-with-tolerance, last-number, exact-line, regex, and a boolean matcher driven by explicit expected/forbidden token lists rather than a fixed Ukrainian keyword set.

#### Scenario: Numeric matcher uses tolerance, not substring

- **WHEN** the expected value is `3.5` with tolerance `0.01` and the student prints `Частка: 3.5`
- **THEN** the matcher passes; and **WHEN** the student prints `3.49` it passes within tolerance; and **WHEN** the student prints `35` it fails

#### Scenario: Boolean matcher avoids stray-digit false positives

- **WHEN** a "is even" case for input `4` expects a truthy answer and the student prints `Число парне`
- **THEN** the matcher passes on the explicit truthy token; and **WHEN** the student prints `Число непарне` the truthy matcher fails instead of matching an unrelated digit or substring

### Requirement: File-fixture harness for file-I/O assignments

The library SHALL provide a harness that enables checking file-I/O assignments despite the read-only `/submission` mount. The harness SHALL copy the student solution into a writable working directory under `/tmp`, seed declared input fixture files there, run the student program with that directory as the working directory, and expose the produced output files for assertion.

#### Scenario: Seed input file, run, assert on output file

- **WHEN** a checker seeds `input.txt` with known content, runs the student program in the `/tmp` workdir, and the program writes `output.txt`
- **THEN** the harness reads back `output.txt` and the checker can assert on its parsed or normalized content

#### Scenario: Student writes are isolated from the real submission

- **WHEN** the student program writes or overwrites files during a fixture run
- **THEN** the writes happen only in the `/tmp` workdir copy and the read-only `/submission` mount and `/plugin` tree are never modified

### Requirement: Structured-content comparison

For assignments whose output is structured data (JSON, CSV), the library SHALL compare parsed content rather than raw bytes, so that key ordering, whitespace, `indent`, and `ensure_ascii` differences do not cause false failures.

#### Scenario: JSON compared by parsed value

- **WHEN** the expected result is a JSON object and the student writes the same object with different key order and indentation
- **THEN** the comparison passes because the parsed structures are equal

### Requirement: Language-agnostic declarative test format

The system SHALL support defining stdin/stdout test cases as data (a YAML spec) executed by a single generic runner, so that a subject in any language can be auto-checked without writing a per-assignment checker. The generic runner SHALL honor the same sandbox contract and emit the same `result.json`.

#### Scenario: A YAML-only assignment is checked with no custom code

- **WHEN** an assignment provides only a YAML file of cases (command, stdin, expected, points) and references the generic runner as its `check_command`
- **THEN** the runner executes each case against the submitted program and produces a valid `result.json`, with no assignment-specific Python written

#### Scenario: Cross-language interface is documented

- **WHEN** a plugin author for a non-Python subject reads the checker-contract documentation
- **THEN** it specifies the `/submission` (read-only), `/plugin` (read-only), `/tmp` (writable), `/output` (read back), `VARIANT` env, `argv[1]=/submission`, `exit 0`, and `result.json` schema interface needed to implement a checker in any language

### Requirement: pythonBasics lab checkers

The pythonBasics subject SHALL provide precise, high-coverage checkers for the cleanly auto-checkable labs, built on the shared library. This SHALL include labs 2 and 4 in full, labs 5 and 6 in full (file-I/O and JSON/CSV/Excel, using the fixture harness and the image's `pandas`/`openpyxl`), the unambiguous variants of labs 3, 7, and 9, and a tightened lab1. Each implemented variant SHALL be tested across edge cases (negatives, zeros, large values, float tolerance, and relevant boundaries).

#### Scenario: Lab 2 variant is checked across edge cases

- **WHEN** a student submits a lab 2 variant (e.g. factorial) and the checker runs
- **THEN** multiple cases including boundary inputs are executed and the score reflects how many cases passed, scaled to the variant's points

#### Scenario: Lab 1 tightened matching rejects spurious output

- **WHEN** a lab 1 variant program prints unrelated numbers or generic text that previously triggered a false positive
- **THEN** the tightened matcher fails the case rather than awarding points for incidental matches

#### Scenario: File-I/O lab is checked with seeded fixtures

- **WHEN** a student submits a lab 5 or lab 6 variant and the checker runs with seeded input file fixtures
- **THEN** the student program runs in the writable workdir, its produced output file is read back and compared by normalized/parsed content, and the score reflects correctness

### Requirement: Task-amendment proposals for non-checkable tasks

For coursework tasks that cannot be deterministically auto-checked as written, the system SHALL provide a short amendment proposal document saved alongside that assignment inside the plugin. Each proposal SHALL identify the ambiguity (undefined I/O contract, free-form output, statefulness, or non-determinism) and specify the minimal task-text change that makes the task checkable.

#### Scenario: Ambiguous task gets an amendment doc

- **WHEN** a maintainer opens lab 8, lab 6 variant 8, or an ambiguous variant of labs 3, 5, 7, or 9
- **THEN** an amendment proposal file exists alongside the assignment describing how to pin the input source, output destination/format, and resolve the ambiguity so an auto-checker can be written; for file-I/O labs that ARE implemented, the doc also records the pinned I/O contract (filenames, format) the checker assumes

#### Scenario: Non-deterministic task is flagged

- **WHEN** a task's reference behavior depends on the current date or other non-deterministic state (e.g. computing age from `datetime.now()`)
- **THEN** the amendment proposal requires the variable input (e.g. a reference year) to be supplied as input so output becomes deterministic
