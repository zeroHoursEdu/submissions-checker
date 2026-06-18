## Why

The platform now has one working subject (pythonBasics) proving the subject-per-repo + custom-image + checklib model. To validate that the model is genuinely language-agnostic — and to give the C++ course auto-checking — we add a second subject, **cppBasics**, plus auto-generated **quizzes** from the course docs (the first use of the engine's quiz feature in a real subject).

Reading all 7 C++ lab PDFs surfaced a hard constraint: every lab is a **Windows-only** program — Win32 GUI (lab1), GDI graphics (lab2), Win32 multithreading (lab3), Winsock client-server (lab4), mailslots/memory-mapped-files/named-pipes (labs 5–7). They use `windows.h`, GUI message loops, machine-dependent `GetSystemMetrics`, multiple processes, and MSVC-isms (`stdafx.h`, `sprintf_s`, `_getch`). **None can be compiled or run on the Linux checker as stdin→stdout programs.** Per the user's decision, we do not try to run the Windows originals; instead each lab is **adapted into the most similar console C++ task that teaches the same topic** and is deterministically compilable + runnable with `g++` on Linux. The original PDFs stay as reference.

## What Changes

- **New standalone subject repo** `/home/vampir/petProjects/cppBasicSubject` (its own git repo), symlinked into the monorepo as `plugins/cppBasics` (loader follows symlinks; `plugins/` is gitignored). `subjectCode: cppBasics`.
- **New custom Docker image** (`Dockerfile`): `python:3.12-slim` + `build-essential` (g++) + `checklib` baked in (`import checklib`). No pandas/MinGW. Referenced at each assignment's `common.sandbox.image`.
- **checklib gains a C++ adapter**: `compile_cpp()` (build `solution.cpp` with `g++ -std=c++17 -O2 -pthread`) and a `run_cpp_cases()` driver that compiles **once** (compile failure → all tests fail with the compiler error) then runs the binary per `Case` over stdin, reusing the existing precise matchers. `REQUIRED_FILE` becomes per-subject (`solution.cpp`). The shared library thus supports both interpreted and compiled languages.
- **Seven adapted console tasks** (labs 1–7), each topic-preserving and Linux-runnable, with deterministic stdin→stdout checkers and edge-case coverage: lab1 formatted metrics report (I/O + formatting), lab2 numeric geometry (loops + math), lab3 `std::thread` parallel compute (the closest direct port), lab4 request/response protocol in one process (client-server message model), labs 5–7 producer-consumer / message-queue / echo-loop (IPC semantics). Each lab ships a `TASK.md` with the adapted statement (Ukrainian, same topic).
- **Per-lab quizzes** generated from the docs: a `quiz:` block per assignment (`review_mode: tests_then_quiz`) with ~8–10 single/multiple-choice questions built from each lab's Win32/GDI/threads/sockets/IPC concepts and control questions. Verified to need **no engine change** — the student quiz route reads `assignments.<lab>.quiz` from the pinned plugin config; questions carry `points`.
- **Docs**: `CONTRACT.md` + `README.md` for the C++ repo (build/push image, run locally, wire in), with local + in-container verification.

## Capabilities

### New Capabilities
<!-- None — this reuses and extends the existing assignment-checking capability. -->

### Modified Capabilities
- `assignment-checking`: extend with (1) compiled-language support — a compile-then-run checker flow where a compile failure deterministically fails all tests with the compiler error, and `REQUIRED_FILE` varies per subject; (2) quiz-from-docs — a subject may attach a config-defined quiz to an assignment (tests gate first, then quiz), read from the pinned plugin config with no engine change.

## Impact

- **New repo** (outside the monorepo): `/home/vampir/petProjects/cppBasicSubject/` — `config.yml`, `Dockerfile`, `pyproject.toml`, `checklib/` (Python + C++ adapter), `assignments/lab1..lab7/` (check.py, TASK.md, original PDF, quiz in config), `CONTRACT.md`, `README.md`, `tests/`.
- **Migration**: symlink `plugins/cppBasics -> ../../cppBasicSubject`; loader upserts on restart. `plugins/` is gitignored, so no monorepo file churn beyond the OpenSpec spec + archived change.
- **New image artifact**: `cppbasics-checker:local` (g++ + checklib), pushed by the teacher to their own registry.
- **Consumes (unchanged)**: the sandbox contract and the quiz route/config schema. No DB / API / migration changes. No breaking changes.
- **Docs**: the `assignment-checking` main spec gains compiled-language + quiz requirements; the C++ repo documents the contract.
