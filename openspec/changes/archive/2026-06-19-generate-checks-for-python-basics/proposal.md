## Why

Today every assignment checker is a ~200-line hand-written Python script that re-implements the same plumbing: spawn the student process, parse stdin/stdout, extract numbers, emit `result.json`. Only `pythonBasics/lab1` exists; labs 2–9 are unchecked. The matching logic is fragile (e.g. `truthy_present`/`numbers_in` produce false positives), there is no way to check file-I/O assignments, and authoring is locked to Python — every new subject in any language would have to repeat the whole boilerplate. Subjects also live inside the monorepo `plugins/` dir, so they cannot be owned, versioned, or scaled independently by the teachers who maintain them.

The real goal is not just "check Python labs". It is to **find what makes automating student-work checking hard and remove it**, so any subject in any programming language can be auto-checked precisely with minimal effort, and **each subject can live in its own repository with its own runtime image**, owned by its teacher. The pythonBasics labs are the proving ground.

## Architecture: subject-per-repo + custom image

This change establishes the scalable packaging model the platform will use going forward:

- **A subject is a standalone git repo** (here: `/home/vampir/petProjects/pythonBasicSubject`) containing its `config.yml`, per-assignment check scripts, fixtures, assignment PDFs, **and a `Dockerfile`** that builds the subject's runtime image. Each teacher owns and manages their own subject repo.
- **A custom Docker image per subject** (hosted in a registry the teacher controls) bundles the interpreter, all third-party dependencies (for pythonBasics: `pandas`, `openpyxl`), and the **`checklib` library baked in** so check scripts simply `import checklib` — no `/plugin/_shared` mount or `sys.path` hacks. `config.yml`'s `image:` field points at this image.
- **The engine is unchanged**: it still mounts the repo at `/plugin` (read-only), runs `tool /plugin/{script_path} /submission`, and reads `/output/result.json`. The repo is loaded locally by symlinking it into the monorepo `plugins/` dir (and in production via the existing ZIP `subject-config-apply` flow).

## What Changes

- **New standalone subject repo** at `/home/vampir/petProjects/pythonBasicSubject` (its own git repo): holds `config.yml`, `assignments/lab1..lab9/`, fixtures, PDFs, the `checklib` source, and a `Dockerfile`. Migrated from (and replaces) the in-monorepo `plugins/pythonBasics`; symlinked into `plugins/` for local engine loading.
- **New custom Docker image** built from the repo's `Dockerfile`: `python:3.12-slim` + `pandas` + `openpyxl` + `checklib` (pip-installed). `config.yml` references it via `image:`. Image name is a placeholder the teacher points at their own registry.
- **New shared checker library** `checklib` (baked into the image, imported as `import checklib`): declarative `Case` model (stdin lines → expected value / regex / predicate, points, float tolerance), a sandboxed subprocess runner with timeout, robust numeric/boolean output matchers that replace the fragile keyword heuristics, a **file-fixture harness** (copy `solution.py` into the writable `/tmp` workdir, seed input files, run, assert on produced files — solving the read-only `/submission` problem for file-I/O labs), parsed JSON/CSV/Excel comparison, and a single canonical `result.json` emitter.
- **New declarative, language-agnostic test format**: define test cases as data (YAML) plus one generic runner, so simple subjects in **any** language need zero checker code. The `/submission`, `/output`, `/tmp`, `/plugin` contract is documented as the cross-language interface.
- **Precise checkers for all cleanly-checkable labs**: implement labs 2 and 4 in full; **labs 5 and 6 in full** (file-I/O and JSON/CSV/Excel, enabled by the custom image's `pandas`/`openpyxl` + the fixture harness); the clean variants of 3, 7 and 9; and **tighten lab1**'s matching to remove false positives. Each variant gets maximal edge-case coverage (negatives, zeros, large values, float tolerance, boundary cases).
- **Task-amendment proposals** (short `.md` files saved alongside each assignment) for tasks that still cannot be auto-checked deterministically — lab 8 (OOP, no I/O contract), lab 6 var 8 (free-form), and the ambiguous variants of 3, 5, 7, 9 — pinning the I/O contract and resolving ambiguities.
- **Optional engine enhancement** (flagged, deferrable): allow separate pass thresholds for the common task vs the variant, so acing the common task can't carry a failed variant.

## Capabilities

### New Capabilities
- `assignment-checking`: The contract and reusable machinery for auto-checking student submissions — declarative test cases, output matchers, the file-fixture harness, parsed structured-data comparison, the language-agnostic data-driven runner, the per-subject repo + custom-image packaging model, and the documented sandbox interface that any subject/language plugin implements.

### Modified Capabilities
<!-- No existing spec's REQUIREMENTS change. The sandbox execution contract in
     subject-management/subject-config-apply is consumed as-is, not modified.
     The optional common/variant-threshold engine change is deferred and would
     be proposed separately if pursued. -->

## Impact

- **New repo** (outside the monorepo): `/home/vampir/petProjects/pythonBasicSubject/` — its own git repo containing `config.yml`, `Dockerfile`, `checklib/` (source + packaging), `assignments/lab1..lab9/` (check scripts, fixtures, PDFs, amendment docs), `CONTRACT.md`, `README.md`.
- **Migration**: the existing `plugins/pythonBasics` content moves into the new repo; the monorepo gets a symlink `plugins/pythonBasics -> ../../pythonBasicSubject` (or the repo is cloned there) so the engine loads it locally. Old monorepo copy removed.
- **New artifact**: a custom Docker image built from the repo `Dockerfile` (`python:3.12-slim` + `pandas` + `openpyxl` + `checklib`), pushed to a teacher-controlled registry; `config.yml` `image:` points at it.
- **Consumes (unchanged)**: the sandbox contract in `src/submissions_checker/services/docker_sandbox.py` and `workers/tasks/check_tasks.py` (`/submission` RO, `/plugin` RO, `/tmp` RW, `/output` read back, `VARIANT` env, `exit 0` + `result.json`). The engine already supports a per-subject `image:` field.
- **No DB / API / migration impact.** No breaking changes to the engine. The optional common/variant-threshold change, if pursued, would touch `check_tasks.py` and is out of scope here.
- **Docs**: `CONTRACT.md` (cross-language checker contract + YAML format) and `README.md` (how to build/push the image, run checks locally, wire the repo into the platform).
