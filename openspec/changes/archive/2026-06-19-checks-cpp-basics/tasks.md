## 1. Standalone C++ subject repo + image

- [x] 1.1 Create `/home/vampir/petProjects/cppBasicSubject/`, `git init`, `.gitignore`, `README.md` (build/push image, run locally, wire in).
- [x] 1.2 Lay out: `config.yml`, `pyproject.toml`, `checklib/`, `assignments/lab1..lab7/`, `Dockerfile`, `CONTRACT.md`, `tests/`. Copy each lab PDF in as `assignment.pdf`; add subject artwork (reuse/placeholder grid.png/main.png).
- [x] 1.3 Write `Dockerfile`: `python:3.12-slim` + `build-essential` (g++), `pip install .` (checklib), non-root user, sanity `import checklib` + `g++ --version`. Placeholder image `cppbasics-checker:local`.
- [x] 1.4 Symlink `plugins/cppBasics -> ../../cppBasicSubject`; verify the loader sees `config.yml` through it.
- [x] 1.5 Build the image locally and confirm `import checklib` + a trivial compile+run inside the container.

## 2. checklib + C++ adapter

- [x] 2.1 Vendor the shared `checklib` (Case, run_cases, matchers, emit, runner, structured, fixtures, yaml_runner) into the repo; `pyproject.toml`.
- [x] 2.2 Parameterize `REQUIRED_FILE` (default `solution.py`); add a way to set it per driver.
- [x] 2.3 Add `checklib/cpp.py`: `compile_cpp(src, out_dir, flags=[-std=c++17,-O2,-pthread]) -> (binary|None, log)` and `run_cpp_cases(cases, submission_dir=None)` — compile once into `/tmp`, on failure emit failed tests with the trimmed compiler error, else run the binary per Case (reuse `run_program(binary, tool=None)` + matchers + `emit`).
- [x] 2.4 Export the C++ API from `checklib/__init__.py`; update `CONTRACT.md` with the compiled-language flow + `solution.cpp` requirement.
- [x] 2.5 Write `assignments/validate.py` (shared): `solution.cpp` exists, non-empty, and compiles (`compile_cpp`); on failure write `/output/validate_error.txt`.

## 3. Adapted tasks + checkers (labs 1–7)

- [x] 3.1 Lab 1 — formatted metrics report: `check.py` (compile+run, stdin→stdout, numeric/format cases) + `TASK.md` (UA, ties to system-info topic).
- [x] 3.2 Lab 2 — numeric geometry (areas/perimeters, sin/cos samples, polygon): `check.py` (float tolerance, sequences) + `TASK.md`.
- [x] 3.3 Lab 3 — `std::thread` parallel compute (factorial / Taylor sin / parallel sum, joined, deterministic): `check.py` (`-pthread`) + `TASK.md`.
- [x] 3.4 Lab 4 — request/response protocol over stdin (commands → responses, `exit` ends): `check.py` + `TASK.md`.
- [x] 3.5 Lab 5 — message-queue producer/consumer over stdin→stdout: `check.py` + `TASK.md`.
- [x] 3.6 Lab 6 — shared-buffer accumulator (mmap analogue): `check.py` + `TASK.md`.
- [x] 3.7 Lab 7 — echo-until-`exit` (named-pipe analogue; matches the lab's own console example): `check.py` + `TASK.md`.
- [x] 3.8 Add edge-case coverage per lab (negatives, zeros, boundaries, float tolerance); confirm a wrong solution scores < max and a correct one scores full.

## 4. Per-lab quizzes (from the docs)

- [x] 4.1 Add a `quiz:` block to each lab in `config.yml` (`review_mode: tests_then_quiz`, `pass_threshold_pct`, `max_quiz_attempts`, `shuffle_questions`, `shuffle_options`, `questions_to_send`, `show_correct_answers_after`).
- [x] 4.2 Author ~8–10 questions per lab from that lab's concepts + control questions, each with `type`, `text`, `choices[].is_correct`, `points`. Cover: Win32 app/message loop (1), GDI/DC (2), threads/mutex/semaphore/event (3), sockets/TCP-UDP/Winsock seq (4), mailslots (5), memory-mapped files (6), pipes named/anonymous (7).
- [x] 4.3 Validate quiz blocks parse and each question carries `points` and exactly the intended correct choice(s).

## 5. Validation & wrap-up

- [x] 5.1 Local test harness (`tests/`): compile+run sample correct & incorrect `solution.cpp` per lab via `checklib`, assert scores; run inside the built image to prove `import checklib` + g++ path.
- [x] 5.2 Validate `config.yml` parses, every assignment references valid `validate_command`/`check_command` paths and the correct `image`, and quiz blocks are well-formed.
- [x] 5.3 Commit the `cppBasicSubject` repo with a meaningful initial commit.
- [x] 5.4 Sync the `assignment-checking` delta into main specs; run `openspec validate checks-cpp-basics --strict` and fix issues.
