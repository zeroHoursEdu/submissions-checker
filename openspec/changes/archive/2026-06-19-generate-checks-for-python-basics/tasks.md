## 1. Standalone subject repo + custom image

- [x] 1.1 Create `/home/vampir/petProjects/pythonBasicSubject/`, `git init`, add `.gitignore` and a `README.md` (what the repo is, how to build/push the image, how to run checks locally, how to wire into the platform).
- [x] 1.2 Lay out the repo: `config.yml` at root, `assignments/lab1..lab9/`, `checklib/` (source + `pyproject.toml`), `Dockerfile`, `CONTRACT.md`. Copy `grid.png`/`main.png` and each lab PDF into place.
- [x] 1.3 Write the `Dockerfile`: `FROM python:3.12-slim`, install `pandas` + `openpyxl`, `COPY checklib/` and `pip install` it, so `import checklib` works in the sandbox. Set a placeholder image name (e.g. `pythonbasics-checker:local`).
- [x] 1.4 Set `config.yml` `image:` to the image; document overriding it with a registry tag in `README.md`.
- [x] 1.5 Symlink (or clone) the repo into the monorepo as `plugins/pythonBasics` and verify the plugin loader loads it; remove the old in-monorepo `plugins/pythonBasics` content. If symlink isn't followed, fall back to clone and note it.
- [x] 1.6 Build the image locally (`docker build`) and confirm it runs `python3 /plugin/assignments/lab1/check.py /submission` with `import checklib` succeeding.

## 2. Shared checker library (`checklib`)

- [x] 2.1 `checklib/__init__.py` public API: `Case`, `run_cases`, matchers, fixture harness, structured-compare, emitter; `pyproject.toml` for pip install.
- [x] 2.2 Sandboxed subprocess runner (`run_program`): launch student program with stdin lines, capture stdout/stderr/exit code, per-case timeout, safe failure messages.
- [x] 2.3 `Case` model and `run_cases(cases, solution_path)` driver that builds the engine `tests[]` and writes `/output/result.json`, then exits 0.
- [x] 2.4 Matchers: `expect_number(value, tol)`, `expect_last_number`, `expect_line`, `expect_regex`, `expect_bool(truthy, falsy)` — precise, no global keyword set.
- [x] 2.5 Structured-content comparison: `compare_json`, `compare_csv`, `compare_xlsx` (parsed via pandas), `normalize_text`.
- [x] 2.6 File-fixture harness `with_workdir(solution_path, fixtures)`: copy solution to `/tmp/work`, seed input files, run with that cwd, return produced files.
- [x] 2.7 `run_yaml` entrypoint: generic runner that reads a `cases.yml` and grades stdin→stdout (and simple file) cases with no per-assignment code.
- [x] 2.8 `CONTRACT.md`: cross-language sandbox interface (`/submission` RO, `/plugin` RO, `/tmp` RW, `/output`, `VARIANT`, `argv[1]`, exit 0, `result.json` schema) + the YAML format + the "bake checklib into your image" convention.

## 3. Tighten and migrate Lab 1

- [x] 3.1 Rewrite `lab1/check_common.py` on `checklib` (4-op calculator, 40 pts) with tolerance-based numeric matching.
- [x] 3.2 Rewrite `lab1/check.py` on `checklib`; replace `truthy_present`/`falsy_present` with `expect_bool` explicit tokens and `expect_number` for numeric variants (60 pts, 10 variants).
- [x] 3.3 Add edge-case coverage; verify tightened matchers reject prior false positives (stray digits, incidental substrings).

## 4. Lab 2 (conditionals & loops)

- [x] 4.1 Add the lab2 block to `config.yml` (common + 10 variants, points mirroring lab1).
- [x] 4.2 Implement all 10 variant checkers (arithmetic/geometric progression, factorial, primes, Fibonacci, sign, max-of-3, skip-multiples, multiplication table, leap year) with edge cases; pin output formats; float tolerance for geometric progression.
- [x] 4.3 Implement the lab2 common-task checker, or document single-check scoring if no common task.

## 5. Lab 4 (functions)

- [x] 5.1 Add the lab4 block to `config.yml`.
- [x] 5.2 Implement all 10 variant checkers (perimeter, factorial, °C→°F, km→m, h→min, mean, square area, max-of-3, in→cm, vowel count) with float tolerance; pin vowel charset+case for variant 10.

## 6. Lab 5 (text files — file-I/O via fixtures)

- [x] 6.1 Add the lab5 block to `config.yml`; pin the file I/O contract (e.g. `input.txt` → `output.txt`) and record it in `lab5/AMENDMENTS.md`.
- [x] 6.2 Implement checkers using `with_workdir` + seeded fixtures for the deterministic variants (1,2,3,4,7,8,9); assert on produced files via normalized/parsed compare.
- [x] 6.3 Write `lab5/AMENDMENTS.md` for the genuinely under-specified variants (e.g. 5 word-presence message, 6 word-replace params, 10 branch-on-existence).

## 7. Lab 6 (JSON / CSV / Excel — file-I/O via fixtures)

- [x] 7.1 Add the lab6 block to `config.yml`; pin fixed input fixtures (`students.json`/`.csv`/`.xlsx`) and the new-record values; record the contract in `lab6/AMENDMENTS.md`.
- [x] 7.2 Implement checkers for the deterministic variants (1,2,3,4,5,6,7,9,10) using `compare_json`/`compare_csv`/`compare_xlsx`.
- [x] 7.3 Write `lab6/AMENDMENTS.md` for var 8 (free-form course content) and any Excel-specific notes.

## 8. Unambiguous variants of Labs 3, 7, 9

- [x] 8.1 Lab 3 (lists/dicts): add config block; implement clean variants (1,2,4,5,6,7,9) with pinned stdin input convention + output format.
- [x] 8.2 Lab 7 (exceptions): add config block; implement strong stdin→stdout variants (1,2,4,7,10) asserting on exception-handling behavior (branch taken, no crash), not message text.
- [x] 8.3 Lab 9 (regex): add config block; implement clean variants (1,8,10) and workable ones with pinned contracts (2,3,4,7,9).

## 9. Task-amendment proposals for remaining non-checkable tasks

- [x] 9.1 Lab 8 (OOP): write `lab8/AMENDMENTS.md` defining an I/O contract per variant; flag stateful/CRUD/free-form (4,5,7,9) and the non-deterministic age variant (3 → require reference year as input).
- [x] 9.2 Labs 3/7/9: write `AMENDMENTS.md` for the deferred ambiguous variants (lab3: 3,8,10; lab7: 3,5,6,8,9; lab9: 5,6).

## 10. Validation & wrap-up

- [x] 10.1 Add a local harness/script (or documented steps) to run a sample correct and incorrect `solution.py` against each implemented checker (inside the built image) and confirm scores.
- [x] 10.2 Validate `config.yml` parses and every assignment block references valid `validate_command`/`check_command` paths and the correct `image`.
- [x] 10.3 Commit the subject repo (`pythonBasicSubject`) with an initial meaningful commit.
- [x] 10.4 Run `openspec validate generate-checks-for-python-basics --strict` and fix any issues.
