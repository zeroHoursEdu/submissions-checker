# Plugin Authoring Guide

This guide explains how to create a subject plugin — a directory containing a `config.yml` and test scripts that the system uses to check student submissions automatically.

---

## Directory Structure

```
plugins/
  <subjectCode>/
    config.yml
    grid.png                 ← optional: subject thumbnail on the subjects grid (400×250px)
    main.png                 ← optional: subject banner on the subject page (1200×400px)
    assignments/
      <assignmentCode>/
        validate.py          ← optional: checks file structure before testing
        check.py             ← required: runs tests, writes /output/result.json
        assignment.pdf       ← optional: downloadable content file for students
        variants/
          "1"/
            check.py         ← optional: override check script for variant 1
          "2"/
            check.py
    docs/                    ← optional: your own documentation and examples
```

Place this directory alongside the application. It is gitignored and loaded at startup.

---

## `config.yml` Reference

```yaml
# subjectCode: used ONLY to match/upsert this subject in the database.
# All other fields (name, description, assignments) are the canonical source of truth.
subjectCode: mathMethods

name: "Mathematical Methods"
description: "Linear algebra and numerical methods for software engineers."

# Subject artwork (optional). Filenames are relative to this plugin's root directory.
# gridPicture: recommended 400×250px, shown on the subjects grid card.
# mainPicture: recommended 1200×400px, shown at the top of the subject page.
# Accepted formats: JPEG, PNG, WebP.
gridPicture: grid.png
mainPicture: main.png

assignments:
  homework1:                     # assignment code — must be unique within the subject
    title: "Homework 1 — Matrix Operations"
    description: "Implement matrix multiplication and inversion."
    deadline: "2026-06-15T23:59:00"   # ISO 8601, treated as UTC
    min_grade: 0
    max_grade: 100
    review_mode: tests_only     # see Review Modes below
    late_policy: block          # block | allow
    max_submissions: 3          # optional; omit for unlimited
    variants_required: false    # if true, student must have a variant assigned before submission
    # Assignment content files (optional). Students see download links on the assignment page.
    # Filenames are relative to assignments/<assignmentCode>/ inside the plugin directory.
    # Any file type is accepted (PDF, DOCX, ZIP, …).
    contentFiles:
      - filename: assignment.pdf
        displayName: "Lab Assignment (PDF)"
    sandbox:
      image: python:3.12-slim
      tool: python3
      validate_command: assignments/homework1/validate.py   # optional
      check_command: assignments/homework1/check.py         # required
      memory: "256m"            # optional, default "256m"
      cpus: 0.5                 # optional, default 0.5
      timeout_seconds: 30       # optional, default 30
      show_case_names_to_student: false        # default false
      show_case_descriptions_to_student: false # default false
    variants:                   # optional; per-variant command overrides
      "1":
        check_command: assignments/homework1/variants/1/check.py
      "2":
        check_command: assignments/homework1/variants/2/check.py
```

### Review Modes

| `review_mode`               | After tests pass, the submission goes to…              |
|-----------------------------|--------------------------------------------------------|
| `tests_only`                | COMPLETED immediately                                  |
| `tests_then_ai`             | AI review → COMPLETED                                  |
| `tests_then_teacher`        | AWAITING_TEACHER_REVIEW → teacher grades               |
| `tests_then_ai_then_teacher`| AI review → AWAITING_TEACHER_REVIEW → teacher grades   |
| `tests_then_quiz`           | Quiz sent to student                                   |

---

## Sandbox Security Model

Every submission check runs inside an isolated Docker container:
- **No network access** (`--network none`)
- **Read-only filesystem** (`--read-only`), only `/tmp` and `/output` are writable
- **Resource limits**: configurable memory, CPU, and PID limits
- **Timeout**: configurable; default 30 seconds — container is killed on timeout
- **No new privileges** (`--no-new-privileges`)

Mounts inside the container:
- `/submission` — extracted student ZIP (read-only)
- `/plugin` — your plugin directory (read-only)
- `/output` — where scripts write their results (writable)

---

## Validate Script Contract

The validate script runs first to check that the student submitted the right files.

**Arguments**: `<tool> /plugin/<validate_command> /submission`  
**Exit code**: 0 = OK, non-zero = invalid  
**On failure**: write a human-readable message to `/output/validate_error.txt`

Example `validate.py`:
```python
import sys
from pathlib import Path

submission_dir = Path(sys.argv[1])
required = ["solution.py", "report.pdf"]
missing = [f for f in required if not (submission_dir / f).exists()]
if missing:
    Path("/output/validate_error.txt").write_text(
        f"Missing required files: {', '.join(missing)}"
    )
    sys.exit(1)
```

---

## Check Script Contract

The check script runs the actual tests.

**Arguments**: `<tool> /plugin/<check_command> /submission`  
**Environment**: `VARIANT` env var is set if the student has a variant assigned  
**Must write**: `/output/result.json`  
**Exit code**: 0 = successfully wrote result (even if tests fail), non-zero = technical failure (system retries)

### `result.json` Schema

```json
{
  "passed": false,
  "score": 40,
  "max_score": 100,
  "tests": [
    {
      "name": "test_matrix_multiply",
      "passed": true,
      "message": "",
      "description": "Basic 2x2 matrix multiplication"
    },
    {
      "name": "test_edge_cases",
      "passed": false,
      "message": "Expected [[1,0],[0,1]] but got [[0,0],[0,0]]",
      "description": "Identity matrix and zero matrix edge cases"
    }
  ]
}
```

- `name` — shown to teacher always; shown to student only if `show_case_names_to_student: true`
- `description` — shown to teacher always; shown to student only if `show_case_descriptions_to_student: true`
- `message` — shown to both teacher and student (keep it informative but not a solution giveaway)
- Student always sees the summary: "X / Y tests passed"

Example `check.py`:
```python
import json
import sys
from pathlib import Path

submission_dir = Path(sys.argv[1])
output = Path("/output/result.json")
variant = __import__("os").environ.get("VARIANT")

# Import student code
sys.path.insert(0, str(submission_dir))
tests = []

def run_test(name, description, fn):
    try:
        fn()
        tests.append({"name": name, "passed": True, "message": "", "description": description})
    except AssertionError as e:
        tests.append({"name": name, "passed": False, "message": str(e), "description": description})

# Load student solution
try:
    from solution import multiply_matrices
except ImportError as e:
    output.write_text(json.dumps({"passed": False, "score": 0, "max_score": 100, "tests": [
        {"name": "import_check", "passed": False, "message": str(e), "description": "Solution imports correctly"}
    ]}))
    sys.exit(0)

def test_basic():
    result = multiply_matrices([[1, 2], [3, 4]], [[5, 6], [7, 8]])
    assert result == [[19, 22], [43, 50]], f"Expected [[19,22],[43,50]] got {result}"

run_test("test_basic_multiply", "Basic 2×2 matrix multiplication", test_basic)

passed_count = sum(1 for t in tests if t["passed"])
output.write_text(json.dumps({
    "passed": passed_count == len(tests),
    "score": round(passed_count / len(tests) * 100),
    "max_score": 100,
    "tests": tests,
}))
```

---

## Variants

Variants allow different problem instances per student (preventing copy-paste).

1. Set `variants_required: true` in the assignment config.
2. Add a `variants:` block mapping variant IDs to command overrides.
3. Use the teacher portal to download the enrollment CSV template (includes variant columns) and distribute it to students.
4. Students fill in their names and variant numbers; teacher imports the filled CSV.
5. The `VARIANT` env var is set inside the sandbox so your check script can select the right test data.

---

## Adding a New Subject

1. Create `plugins/<subjectCode>/config.yml` with the full config.
2. Write validate and check scripts for each assignment.
3. Restart the application — the plugin loader runs at startup and upserts the subject and assignments into the database.
4. Enroll students via the teacher portal (use the subject's CSV template).

---

## Updating a Subject

Edit `config.yml` and restart. The loader detects the changed content hash and inserts a new config version. In-progress submission checks continue using the previous version; new checks use the updated one.

---

## Local Testing

Test your scripts before deploying by running the sandbox manually:

```bash
# Create a fake student submission
mkdir /tmp/test_submission
echo "def multiply_matrices(a, b): return [[1]]" > /tmp/test_submission/solution.py

# Create a temp output dir
mkdir /tmp/test_output

# Run the check script (mirrors exactly what the system does)
docker run --rm \
  --network none \
  --memory 256m --cpus 0.5 \
  --pids-limit 100 \
  --no-new-privileges \
  --read-only --tmpfs /tmp:rw,size=64m \
  -v /tmp/test_submission:/submission:ro \
  -v $(pwd)/plugins/mathMethods:/plugin:ro \
  -v /tmp/test_output:/output:rw \
  python:3.12-slim python3 /plugin/assignments/homework1/check.py /submission

# Inspect result
cat /tmp/test_output/result.json
```
