## Context

The submissions-checker runs student code in a locked-down Docker sandbox; a subject is a standalone repo (config + check scripts + `checklib` + Dockerfile) symlinked into `plugins/`, with its own runtime image. pythonBasics is the reference subject. The sandbox contract is fixed (`/submission` RO, `/plugin` RO, `/tmp` RW, `/output` read back, `VARIANT` env, `argv[1]=/submission`, exit 0 + `result.json`; the engine recomputes score from merged test points vs `min_pass_score`).

This change adds **cppBasics**. Reading all 7 C++ lab PDFs showed every lab is Windows-only (Win32 GUI, GDI, Win32 threads, Winsock, mailslots, mmap, named pipes), using `windows.h`, machine-dependent `GetSystemMetrics`, multiple processes, and MSVC-isms. None compile or run on the Linux checker. The user chose to **adapt each lab into the closest console C++ task on the same topic** rather than attempt the Windows originals. Separately, the user wants **quizzes generated from the docs** — the engine already supports config-defined quizzes (verified: `student_quiz` reads `assignments.<lab>.quiz` from the pinned config; questions need `points`), so no engine change is required.

## Goals / Non-Goals

**Goals:**
- Prove the subject model is language-agnostic by adding a compiled-language subject reusing `checklib`.
- Add a C++ compile-then-run flow to `checklib` (compile once; compile error fails tests cleanly; run binary per case; reuse existing matchers).
- Ship 7 adapted, deterministic console tasks teaching the same topics as the originals, each with a checker + `TASK.md`.
- Ship a per-lab quiz from the doc facts (`tests_then_quiz`).
- Keep the engine, DB, API, and migrations unchanged.

**Non-Goals:**
- Not running/compiling the Windows originals; not using MinGW or a Windows runner.
- Not faithfully reproducing GUI/graphics/networking behavior — only the underlying topic, in a runnable console form.
- Not pushing the image to a real registry (teacher owns that); repo ships the `Dockerfile` + placeholder name.
- Not changing the quiz engine or adding new question types beyond what the route supports.

## Decisions

**1. Adapt, don't emulate.** Each Windows lab maps to a console task preserving the topic:
| Lab | Original (Windows) | Adapted console task (Linux, g++) |
|-----|--------------------|-----------------------------------|
| 1 | Win32 system-info GUI | read inputs → print a formatted **metrics report** (areas, mm conversions, aspect ratio). I/O + formatting. |
| 2 | GDI graphics | **numeric geometry**: shape area/perimeter, sample sin/cos at N points, polygon vertices. loops + math. |
| 3 | Win32 multithreading | **`std::thread`** parallel compute (factorial, Taylor sin, parallel sum, producer/consumer w/ `std::mutex`), joined → deterministic output. Closest direct port. |
| 4 | Winsock client-server | **request/response protocol** in one process: read commands from stdin, respond per protocol. client-server message model. |
| 5–7 | mailslots / mmap / pipes (IPC) | **producer-consumer / message-queue / echo-loop** over stdin→stdout (lab7's own example is already console echo-until-`exit`). IPC semantics. |
*Rationale:* the user asked for the most similar runnable task; this keeps each lab's learning objective while making grading deterministic. Each adaptation is written into `TASK.md` (Ukrainian) so students get a clear statement; the PDF stays for reference.

**2. C++ support in `checklib` via a compile-then-run adapter.** Add `cpp.py`: `compile_cpp(src, out, flags=["-std=c++17","-O2","-pthread"]) -> (binary|None, log)` and `run_cpp_cases(cases, submission_dir)` that (a) locates `solution.cpp`, (b) compiles once into the writable `/tmp`, (c) if compile fails, emits one failed test per case (or a single compile-failure test) carrying the trimmed compiler log, (d) else runs the binary per `Case` via the existing `run_program(binary, stdin, tool=None)` and reuses all matchers + `emit`. *Alternative:* a separate C++-only library — rejected; reuse keeps matchers/emitter/contract identical and proves language-agnosticism. *Decision:* `REQUIRED_FILE` is parameterized (default `solution.py`; C++ driver uses `solution.cpp`).

**3. checklib is duplicated per repo, extended in place.** Per the subject-per-repo model, cppBasicSubject vendors its own `checklib` copy (now including `cpp.py`). *Trade-off:* duplication with pythonBasics' copy. *Mitigation:* the C++ additions are additive and the shared core stays identical; a future step could publish `checklib` as a package both repos install. Noted, not done here.

**4. Quizzes are pure config.** Each lab gets a `quiz:` block (`questions[]` with `type`/`text`/`choices[].is_correct`/`points`, plus `pass_threshold_pct`, `max_quiz_attempts`, `shuffle_questions`, `shuffle_options`, `questions_to_send`, `show_correct_answers_after`). `review_mode: tests_then_quiz` → compile+run tests gate first, then quiz. Built from each lab's concept facts + the PDFs' control questions. *Verified:* loader persists the full config (incl. `quiz`) in `SubjectPluginConfig.config`; `student_quiz` reads it. No engine change.

**5. Image: g++ + checklib, no extras.** `python:3.12-slim` + `build-essential`, `pip install .` (checklib). Native console C++ only → no MinGW, no pandas. Smaller, simpler than the pythonBasics image.

**6. Scoring.** Each lab: tests total 100 (compile+run cases), `min_pass_score` ~60, then quiz with its own `pass_threshold_pct`. No common/variant split needed (the adapted tasks are single tasks; optional variants via `VARIANT` where natural).

## Risks / Trade-offs

- **Adapted tasks diverge from the printed coursework** → Mitigation: `TASK.md` states the adapted task explicitly and ties it to the original topic; teacher can publish `TASK.md` as the canonical statement. The lab4 "protocol" and lab5–7 "IPC" adaptations are the loosest ports — flagged in their `TASK.md`.
- **Compiler error noise / size** → Mitigation: trim the g++ log to a bounded length in the failure message; `result.json` stays < 1 MB.
- **Student uses Windows-only headers** (`windows.h`) in a console submission → Mitigation: it simply won't compile → tests fail with a clear message; `TASK.md` says "standard C++ only, no `windows.h`".
- **Compile/run resource use** (`-O2`, threads) → Mitigation: per-case timeout + the sandbox's memory/cpu/pids limits; `-O2` build is one-time per submission.
- **checklib duplication drift** → Mitigation: additive-only C++ module; revisit packaging when a third subject appears.
- **Non-determinism in threaded tasks** → Mitigation: adapted tasks join all threads and print only final aggregated results; no timing-dependent output asserted.

## Migration Plan

1. Create `cppBasicSubject` repo; vendor `checklib` (+ `cpp.py`); write `Dockerfile`, `config.yml`, 7 labs (`check.py`, `TASK.md`, copied PDF, quiz), docs, tests; `git init` + commit.
2. Build the image locally; verify `import checklib` and a sample compile+run in-container.
3. Symlink `plugins/cppBasics -> ../../cppBasicSubject`; loader upserts on restart. Rollback = remove symlink; no schema/data change.

## Open Questions

- Should `checklib` become a published package shared by both subject repos? (Deferred until a third subject.)
- For labs 5–7 (IPC), is a single producer-consumer adaptation per lab enough, or should each map to a distinct IPC flavor (queue vs shared-buffer vs echo)? (Resolved in design: lab5 message-queue, lab6 shared-buffer/accumulator, lab7 echo-until-exit — distinct flavors.)
