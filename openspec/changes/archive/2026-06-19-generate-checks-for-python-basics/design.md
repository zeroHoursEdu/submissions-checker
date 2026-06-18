## Context

The submissions-checker runs student code in a locked-down Docker sandbox and grades it with per-assignment "check scripts". The contract (verified in `docker_sandbox.py` and `check_tasks.py`) is:

- `docker run --read-only --network none --pids-limit 100`, `--tmpfs /tmp:rw,size=64m`.
- Mounts: `/submission` (extracted student ZIP, **read-only**), `/plugin` (the whole plugin tree, **read-only**), `/output` (writable, read back after exit; files <1 MB).
- Invocation: `tool /plugin/{script_path} /submission` → script gets `sys.argv[1] == "/submission"` and, when assigned, a `VARIANT` env var.
- The script MUST `exit 0` (non-zero is treated as a technical failure and raises) and write `/output/result.json` containing a `tests` array. The engine **ignores** script-provided totals and recomputes `score`/`max_score` by summing `points_earned`/`max_points` across the merged common+variant test lists, then passes if `(score/max)*100 >= min_pass_score`.

Only `pythonBasics/lab1` is implemented. Its `check.py`/`check_common.py` duplicate ~200 lines of subprocess + parsing + JSON code and rely on `numbers_in` / `truthy_present` / `falsy_present` heuristics that false-positive on stray digits and substrings. Labs 2–9 have no checkers. Reading all nine PDFs shows three task families: clean stdin→stdout (labs 1,2,4, parts of 3/7/9), file-I/O (labs 5,6, parts of 7), and under-specified/OOP/free-form (labs 8, parts of 3/6/9). The current mechanism can only address the first family, and only by copy-pasting boilerplate per Python assignment.

Additionally, all subjects currently live inside the monorepo `plugins/` dir, so they cannot be owned, versioned, or scaled independently. This change adopts a **subject-per-repo + custom-image** model: each subject is its own git repo carrying its config, checkers, fixtures, and a `Dockerfile` for a runtime image that bundles its dependencies and `checklib`. The engine already supports a per-subject `image:` field and loads any directory placed under `plugins/`, so this is a packaging/ownership change, not an engine change. The custom image (with `pandas`/`openpyxl`) also unblocks real file-I/O and JSON/CSV/Excel checking, moving labs 5 and 6 from "amendment-only" to fully implemented.

## Goals / Non-Goals

**Goals:**
- Establish the subject-per-repo + custom-image packaging model with pythonBasics as the reference implementation.
- Eliminate checker boilerplate via one shared library (`checklib`) baked into the image and imported directly.
- Make matching precise (tolerance-based numeric compare, explicit boolean tokens, parsed structured-data compare) to kill false positives.
- Enable file-I/O and JSON/CSV/Excel checking despite read-only `/submission` via a `/tmp` fixture workdir harness + bundled `pandas`/`openpyxl`.
- Provide a data-only (YAML) declarative test path + generic runner so non-Python subjects need zero checker code — the language-agnostic win.
- Implement precise, high-coverage checkers for all cleanly checkable labs (1,2,4,5,6 + clean variants of 3,7,9) and tighten lab1.
- For genuinely non-checkable tasks, ship task-amendment proposals alongside the assignment instead of forcing a fragile checker.

**Non-Goals:**
- No changes to the sandbox engine, DB, API, or migrations. The contract is consumed as-is.
- Not implementing checkers for tasks that remain ambiguous as written (lab 8, lab 6 var 8, and the ambiguous variants of 3,5,7,9) — those ship amendment docs.
- Not hosting/pushing the image to a real registry (the teacher owns that); the repo provides the `Dockerfile` and a placeholder image name + build instructions.
- The optional per-section (common vs variant) pass-threshold engine change is out of scope; noted as a follow-up.
- Not auto-grading report prose, screenshots, or free-form text.

## Decisions

**0. Subject packaging: standalone repo + custom image.**
The subject is a self-contained git repo at `/home/vampir/petProjects/pythonBasicSubject` holding `config.yml`, `assignments/`, fixtures, PDFs, `checklib/` source, and a `Dockerfile`. The `Dockerfile` builds `python:3.12-slim` + `pandas` + `openpyxl` + an editable/pip install of `checklib`, producing the subject's runtime image; `config.yml` `image:` points at it (placeholder registry name the teacher overrides). For local engine loading, symlink the repo into the monorepo as `plugins/pythonBasics` (the loader reads any dir there). *Why:* lets each teacher own/version/scale their subject independently and bundle exactly the deps their labs need. *Alternative:* keep subjects in the monorepo — rejected; doesn't scale across teachers. *Risk:* image must be reachable by the engine host; documented in README with a `docker build` local fallback.

**1. Shared library lives in the repo, baked into the image (imported as `import checklib`).**
`checklib/` source sits in the subject repo (so it is versioned with the config and built into the image). The `Dockerfile` `pip install`s it into site-packages, so check scripts just `import checklib` with **no `sys.path` hack and no `/plugin/_shared` mount**. *Why:* the custom-image model makes baking-in the natural, robust choice and removes the loader-scanning risk of a `plugins/_shared` dir entirely. *Alternative considered:* vendor in the repo and `sys.path.insert("/plugin")` — rejected (path-brittle, reintroduces per-repo duplication when other subjects reuse the lib; they can instead `pip install` the same published package or COPY it in their own image).

**2. Declarative `Case` model + `run_cases()` driver.**
A `Case` carries `name`, `description`, `stdin: list[str]`, `points`, and a `matcher` (callable or one of the built-ins). `run_cases(cases, solution_path)` runs each case, builds the test dicts, and writes `result.json`. This collapses the duplicated `main()` in every checker to a list of `Case`s. *Alternative:* a base class to subclass — rejected as heavier than data.

**3. Matchers as explicit, composable predicates.**
Replace `numbers_in/value_present/truthy_present/falsy_present` with: `expect_number(value, tol=…)`, `expect_last_number(...)`, `expect_line(text)`, `expect_regex(pat)`, and `expect_bool(truthy=[...], falsy=[...])` where token lists are supplied per case (no global Ukrainian keyword set). Numeric matching extracts numbers but compares by tolerance against the *expected*, and the boolean matcher requires a declared token to appear and its opposite to be absent — removing the "stray digit / `є ` substring" false positives. *Trade-off:* per-case token lists are more verbose, but precision is the explicit requirement.

**4. File-fixture harness: copy-to-/tmp pattern.**
`with_workdir(solution_path, fixtures={"input.txt": "..."})` copies `solution.py` to `/tmp/work`, writes fixture files, runs the student program with `cwd=/tmp/work`, and returns produced files. This is the only way to let student code that opens `"input.txt"`/writes `"output.txt"` work, since `/submission` is read-only and `/tmp` is the sole writable mount (64 MB). Output files are compared via a `compare_json`/`compare_csv`/normalized-text helper. *Alternative:* ask the engine to mount `/submission` read-write — rejected (engine change, weakens isolation).

**5. Language-agnostic YAML format + generic runner.**
A `cases.yml` (list of `{name, stdin, expect: {number|line|regex|bool}, points, description}`) is executed by `checklib`'s `run_yaml` entrypoint (e.g. `assignments/<lab>/run_yaml.py` or a console-script installed in the image), referenced as an assignment's `check_command`. The student program is launched with the subject's `tool`. Because the runner only speaks the documented sandbox interface (argv, stdin/stdout, files, `result.json`), the same YAML approach works for a C/Java/Node subject by changing only `image`/`tool`. A `CONTRACT.md` documents this interface for plugin authors. *Trade-off:* YAML covers stdin→stdout and simple file cases; genuinely complex grading still drops to a Python `check.py` using the same library — a smooth escape hatch, not a wall.

**6. Scope of implemented checkers vs amendments.**
Implement: lab1 (tighten + migrate), lab2 (all 10), lab4 (all 10), **lab5 (all — file-I/O via fixture harness)**, **lab6 (JSON/CSV/Excel via `pandas`/`openpyxl` + fixtures; parsed-content compare)**, and the unambiguous variants of lab3/7/9. Ship amendment `.md`s for: lab8 (all — OOP, no I/O contract), lab6 var8 (free-form), and the ambiguous variants of lab3 (3,8,10), lab5 (the genuinely under-specified ones, e.g. var5 word-presence message / var10 branch-on-existence), lab7 (3,5,6,8,9 where free-form/ambiguous), lab9 (5,6). Each implemented lab gets a `config.yml` block mirroring lab1's common+variants shape. *Rationale:* the custom image unblocks file/structured checking, so implement everything deterministic; only ship amendments where the task itself is genuinely undefined. For file-I/O labs the amendment doc still records the pinned I/O contract the checker assumes (filenames, format), so the task text can be updated to match.

**7. Per-variant points stay at 60 (variant) + 40 (common) where a common task exists.**
Match lab1's existing weighting so the engine's merged scoring and `min_pass_score` keep working unchanged. Labs without a meaningful common task use a single check totaling 100.

## Risks / Trade-offs

- **Custom image not reachable by the engine host / not pushed yet** → Mitigation: README documents `docker build -t pythonbasics-checker:local .` and setting `image:` to the local tag; placeholder registry name is overridden by the teacher. The engine pulls/uses whatever `image:` resolves to.
- **`checklib` baked into image drifts from repo source** → Mitigation: the image is built FROM the repo (`COPY checklib/ … && pip install`), so a rebuild is the single source of truth; README ties image rebuild to checklib changes. Local check runs use the freshly built image.
- **64 MB `/tmp` cap for fixtures** → Mitigation: keep fixtures small; the lab fixtures are tiny text/JSON/CSV/XLSX. Document the limit.
- **Output-format assumptions vs. what students actually print** → Mitigation: matchers are tolerant (number-anywhere within tolerance, normalized text, parsed JSON/CSV) rather than exact-string; where the task is genuinely undefined we amend the task rather than guess.
- **Excel reproducibility (lab6)** → Mitigation: compare parsed DataFrame content (values, not byte layout); seed `.xlsx` fixtures with `openpyxl`; tolerate column-order/dtype where reasonable.
- **Free-form Ukrainian messages (lab7)** → Mitigation: assert on exception-handling *behavior* (correct branch, no crash, exit code) not message text; documented in lab7 amendments.
- **Symlinking the repo into `plugins/`** → Mitigation: relative symlink `plugins/pythonBasics -> ../../pythonBasicSubject`; if the loader resolves real paths oddly, fall back to cloning the repo into `plugins/`. Verified during apply.

## Migration Plan

1. Create the standalone repo, move/rewrite the lab1 content into it, add labs 2–9, `checklib`, `Dockerfile`, docs; `git init` + initial commit.
2. Build the image locally (`docker build`); teacher later pushes to their registry and updates `image:`.
3. Replace the monorepo `plugins/pythonBasics` with a symlink to the repo so the engine loads it; the loader upserts the new `config.yml` version on restart.
4. Rollback = restore the old `plugins/pythonBasics` directory and revert `config.yml`; no schema/data migration. Existing lab1 submissions are unaffected (tightening only changes future grading; points layout unchanged).

## Open Questions

- Does the loader follow a symlinked `plugins/pythonBasics` cleanly, or should the repo be cloned into `plugins/` instead? Verify during apply.
- Final image registry/name — the teacher supplies it; repo ships a placeholder + build instructions.
- Should the optional common-vs-variant separate threshold be pursued as a follow-up change? (Deferred.)
