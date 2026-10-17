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

Zip this directory (with `config.yml` at the ZIP's root) and upload it through the teacher
portal's "Apply config" form, or `POST` it directly to `/teacher/subjects/apply-config` as an
authenticated teacher. There is no startup scan and no manual file placement — uploading is the
only way a subject's code and config reach the system. **The ZIP itself is the only copy of a
subject's check scripts that matters**: the raw bytes are stored in the database
(`subject_plugin_configs.zip_data`), and a check worker unpacks that stored archive on demand
into a cache directory for the sandbox to mount. Nothing is extracted to a `plugins/` directory
on disk any more, so your ZIP must contain the **full** subject tree — `config.yml` and every
script it references — or a check will fail to find them at run time.

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

# Squads (optional). Students may pair up: one ZIP per squad, the quiz split between
# members, one unified grade. Omit the block to keep every student solo.
squads:
  maxAllowedSize: 2        # 2..6

assignments:
  homework1:                     # assignment code — must be unique within the subject
    title: "Homework 1 — Matrix Operations"
    description: "Implement matrix multiplication and inversion."
    deadline: "2026-06-15T23:59:00"   # ISO 8601, treated as UTC
    min_grade: 0
    max_grade: 100
    review_mode: tests_only     # legacy; see Requirements and source below
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

### Squads

Optional, subject-level. When `squads.maxAllowedSize` is set, students on this subject
may pair up (or form larger groups, up to the configured size) instead of working
solo: one ZIP upload counts for the whole squad, the defence quiz is split into a
disjoint slice per member, and the final grade is one shared mark written to every
member. `maxAllowedSize` must be an integer between 2 and 6; omit the block entirely
to keep the subject solo-only. See [`docs/features/squads.md`](features/squads.md)
for the full rules, teacher/student usage, and the tables involved.

### Requirements and source

An assignment declares **what must be done for the work to count**, not a pipeline. Each
item is independent and may be done in any order (the quiz can be passed before anything is
handed in). The grade appears once every required item is done and is
`min_grade + Σ earned points`, rounded once at the end (squads round up), clamped to
`[min_grade, max_grade]`.

```yaml
lab4:
  min_grade: 0
  max_grade: 16
  source: classroom            # upload | classroom
  requirements:
    - {type: quiz,  key: quiz,     points: 8}
    - {type: score, key: report,   title: "Звіт", max: 5, llm: "rubric text"}
    - {type: score, key: star,     title: "Зірочка", max: 3, optional: true}
    - {type: check, key: accepted, title: "Прийнято викладачем", points: 0}
    # subjects with code:
    - {type: tests,     key: tests, points: 10}
    - {type: ai_review, key: ai,    points: 2}
  quiz: { ... }                # required iff there is a quiz item
```

| `type` | Done when | Points earned |
|---|---|---|
| `quiz` | a passed attempt exists | `points × pct/100` (squad: mean over members) |
| `score` | the teacher saved a value on the board | `0..max` |
| `check` | the teacher ticked it and saved; cannot be unticked | `points` (0 allowed) |
| `tests` | the sandbox check of the latest version finished | `points × score/max_score` |
| `ai_review` | the AI review of the latest version finished | `points × code_mark/100` |

- `optional: true` (any type): not needed for the grade, only adds points.
- `source`: `upload` = the student uploads a ZIP on the portal; `classroom` = the work comes
  from the Classroom sync and the portal shows no upload. Default: `upload` when the list has
  `tests` or `ai_review`, otherwise no hand-in at all.
- `llm:` (on a `score` only) makes the criterion LLM-drafted; the value is the rubric text.

**Validation on apply-config**: `Σ points/max == max_grade - min_grade`; keys match
`[a-z0-9_]+` and are unique; `title` required for `score`/`check`; `check` takes `points`, not
`max`; at most one `quiz` item; a `quiz` item needs a `quiz:` block with questions and a
`quiz:` block needs a `quiz` item; `tests`/`ai_review` with `source: classroom` is refused
("not supported yet"); `review_mode` together with `requirements` is refused.

Rejecting work that has no upload (a Classroom source) is final until a new Classroom version
of the work arrives; the board cannot reopen it.

**Legacy.** `review_mode` is still read and translated into a requirements list by
`requirements.from_config`; do not mix it with `requirements`. Translation: `tests_only`,
`tests_then_ai`, `tests_then_teacher` (adds a `check`), `tests_then_quiz`, `quiz_only`,
`quiz_then_teacher` and the `*_ai_*` variants become `tests` / `ai_review` / `quiz` / `check`
items weighted as the old `grading` block weighted them; `quiz_and_teacher_scores` becomes a
`quiz` (`quiz_points`) plus one `score` per `teacher_criteria` entry, with `source: classroom`
when `llm_grading` is enabled. Grades are identical to the old formulas. Check-free modes need
no `sandbox` block and no `check_command`. New configs should use `requirements`.

#### Legacy `quiz_and_teacher_scores`

The old form of "quiz any time, teacher grades the work" (translated as above):

```yaml
lab3:
  min_grade: 0
  max_grade: 16
  review_mode: quiz_and_teacher_scores
  grading:
    quiz_points: 8
    teacher_criteria:
      - { key: report, title: "Звіт", max: 5 }
      - { key: star,   title: "Завдання з зірочкою", max: 3, optional: true }
  quiz: { ... }
```

Rules: `quiz_points` an integer >= 0, `teacher_criteria` non-empty, keys `[a-z0-9_]+` and
unique, each `max` an integer > 0, and `quiz_points + Σ max == max_grade - min_grade`. There
is no upload; the first quiz open creates an upload-less `QUIZ_ONLY` submission.

##### `llm_grading` — nightly AI draft of the criteria points (optional)

For a `quiz_and_teacher_scores` assignment whose work is submitted in **Google Classroom**
(not on the platform), the platform can fetch the work and have an LLM draft the
`teacher_criteria` points overnight. The teacher still approves every grade.

```yaml
lab3:
  review_mode: quiz_and_teacher_scores
  grading:
    quiz_points: 40
    teacher_criteria:
      - key: report
        title: Звіт
        max: 30
        requirements: |            # required while the criterion is LLM-graded
          Звіт містить мету, хід роботи, скріншоти, висновки.
      - key: oral
        title: Усна частина
        max: 30
        llm: false                 # teacher-only: the LLM leaves it blank
  llm_grading:
    enabled: true
    source: google_classroom       # the only source for now
    task_file: tasks/lab3.md       # path inside the config ZIP; or inline `task:`
    instructions: |                # optional extra rules for the grader
      Варіант студента вказаний на титульній сторінці.
```

- **`requirements`** (criterion key): what a full mark needs, in plain words. The grader
  awards only what it can quote evidence for, and gives 0 for the rest.
- **`llm: false`** (criterion key): the LLM does not score this criterion; its input on the
  board stays for the teacher. Default is `true`.
- **`task_file` / `task`**: the assignment text the grader judges against. `task_file` is read
  from the ZIP at apply time and stored inline, so nothing is read from the ZIP at runtime.
- **Apply config rejects** the assignment when: `llm_grading` is on a mode other than
  `quiz_and_teacher_scores`; `enabled` is not a bool or `source` is not `google_classroom`;
  neither `task` nor `task_file` is given, or the `task_file` is missing from the ZIP or not
  UTF-8 text; an LLM-graded criterion has empty `requirements`; or no criterion is LLM-graded.
- **How it behaves**: a teacher links the Classroom course to the subject and the coursework
  item to this assignment (Операції tab). Each night (03:00 Europe/Kyiv) new or changed
  submissions are fetched (max 10 files of 20 MB per work; larger files are skipped) and
  graded. Drafts appear on the assignment board pre-filled and marked «AI-чернетка», with
  justification, evidence and the files, and **never** reach the student; saving the points
  is the approval. A name-only student match must be confirmed first, and a draft that
  appeared after the page was opened is refused until the page is reloaded.
  See [`docs/deployment.md`](deployment.md#classroom-ingest-and-nightly-llm-grading) for
  the ops setup.

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

## AI Review Block

Used by `tests_then_ai`, `tests_then_ai_then_teacher` and `tests_then_ai_then_quiz`. The
worker sends the README (task) and the student's source files to the configured provider
(`AI_PROVIDER=openai|anthropic`) and stores a structured verdict on the submission.

```yaml
assignments:
  lab3:
    review_mode: tests_then_ai_then_teacher
    ai_review:
      cheating_threshold: 0.6        # default 0.5 — confidence at/above which "cheating" flags
      ai_generated_threshold: 0.7    # default 0.5
      source_extensions: [java, xml] # default: a broad list (py, java, kt, c/cpp/h, cs, js/ts, go, rs, …)
      show_comment_to_student: true  # default false — student sees the AI comment on the assignment page
    grading:
      show_breakdown_to_student: true  # default false — student sees the works/quality/quiz weights
```

| Key | Effect |
|---|---|
| `cheating_threshold`, `ai_generated_threshold` | A verdict is **flagged** when the model says yes *and* its confidence meets the threshold. Under `tests_then_ai_then_quiz` a flagged submission goes to the teacher instead of the quiz. Flagged submissions get a red `AI ·` badge on the assignment board and a highlighted panel on the review page. |
| `source_extensions` | Which files count as code. Case-insensitive, leading dot optional. Files under `build/`, `target/`, `node_modules/`, `.git/` are always skipped; total code sent is capped at 200 k characters. |
| `show_comment_to_student` | The verdict's `comment` is written for the student; it is hidden unless this is `true`. |
| `grading.show_breakdown_to_student` | Shows the grade composition on the student's assignment page. |

The `code_mark` (0–100) feeds the grade through `grading.quality_weight` — see the Grading
section. The teacher always sees the full verdict on `/teacher/submissions/{id}/review`.

## Quiz Block

A quiz lives at `assignments.<code>.quiz` in `config.yml`. It is read from the pinned plugin
config at runtime — there is no quiz code, no separate file, and no database seeding. Questions
and settings are snapshotted onto each attempt when it starts, so editing `config.yml` never
disturbs an attempt already in progress.

```yaml
assignments:
  lab1:
    review_mode: quiz_then_teacher
    quiz:
      # ── selection ──
      questions_to_send: 15        # draw 15 of the pool per attempt (default: all)
      shuffle_questions: true      # default true
      shuffle_options: true        # default true
      # ── passing ──
      pass_threshold_pct: 0.7      # fraction of points needed (default 0.6)
      max_quiz_attempts: 2         # omit for unlimited
      show_correct_answers_after: false   # default false
      # ── timing ──
      time_limit_minutes: 20       # whole-attempt budget (optional)
      question_time_default_seconds: 30   # per-question default (optional)
      anti_cheat: { ... }          # see docs/anti-cheat.md
      questions:
        - type: single_choice
          text: "Which function registers a window class?"
          points: 1
          time_limit_seconds: 45   # overrides the quiz default for this question
          choices:
            - { text: "RegisterClassEx", is_correct: true }
            - { text: "CreateWindow",    is_correct: false }
```

### Quiz-level keys

| Key | Type | Default | Meaning |
|---|---|---|---|
| `questions` | list | — | The question pool. Required. |
| `questions_to_send` | int | all | How many to draw per attempt. Questions marked `required: true` are always drawn first. |
| `shuffle_questions` | bool | `true` | Randomise the order of the drawn questions. |
| `shuffle_options` | bool | `true` | Randomise choice order (correct indices are remapped). |
| `pass_threshold_pct` | float | `0.6` | Fraction of the drawn questions' points needed to pass. |
| `max_quiz_attempts` | int | unlimited | Terminal attempts allowed. Exhausting them fails the submission. |
| `show_correct_answers_after` | bool | `false` | Reveal the correct answers on the result page. Leave `false` when students get more than one attempt from a pool, or the first attempt hands them the answer key. |
| `time_limit_minutes` | int | none | Budget for the whole attempt. Expiry finalises the attempt as `TIMED_OUT`. |
| `question_time_default_seconds` | int | none | Per-question budget applied to every question that does not set its own. |
| `anti_cheat` | map | none | Proctoring and punishment rules, including the optional `camera` block (browser-side face detection, evidence frames) — see `docs/anti-cheat.md`. |

### Question keys

| Key | Type | Meaning |
|---|---|---|
| `type` | string | `single_choice`, `multiple_choice`, `true_false`, `ordering`. Any other type is rejected when the config is applied. |
| `text` | string | The prompt. |
| `points` | int | Weight. Default `1`. |
| `choices` | list | `{text, is_correct}` entries. `single_choice` takes the first `is_correct`; `multiple_choice` requires the exact set. |
| `options` + `correct` | list + int/list | Alternative to `choices`: `correct: 0` for single, `correct: [0, 2]` for multiple. |
| `items` + `correct_order` | list + list | For `ordering`. |
| `correct` | bool | For `true_false`. |
| `required` | bool | Always include this question in every draw. |
| `time_limit_seconds` | int | This question's own clock. Overrides `question_time_default_seconds`. |

### Per-question timing (stepper mode)

If **any** drawn question resolves a `time_limit_seconds` — its own or the quiz default — the
whole attempt switches to **stepper delivery**: one question per page, each with its own
countdown, and **no going back**. A quiz where no question has a limit keeps the familiar
all-questions-on-one-page form, so existing subjects are unaffected.

The clock is the server's, not the browser's:

- The window starts when the server serves the question.
- Closing or reloading the page neither pauses nor resets it. On the next page load, every
  question whose window elapsed in the meantime is recorded as answered-with-zero and flagged
  as timed out — and an expired question's successor starts its window when the expired one
  *ended*, so disappearing for an hour burns the rest of the attempt rather than one question.
- Answering early is not punished: a normal answer restarts the clock at that moment.
- An answer that arrives after its window closed scores zero, even if it was correct.
- Timed-out questions are shown as such on the result page, distinct from wrong answers.

Calibrate the windows generously enough to absorb a page load — 30 s for recall, 45–60 s for
questions that need thought — rather than shaving them to the second.

## Variants

Variants allow different problem instances per student (preventing copy-paste).

1. Set `variants_required: true` in the assignment config.
2. Add a `variants:` block mapping variant IDs to command overrides.
3. Use the teacher portal to download the enrollment CSV template (includes variant columns) and distribute it to students.
4. Students fill in their names and variant numbers; teacher imports the filled CSV.
5. The `VARIANT` env var is set inside the sandbox so your check script can select the right test data.

---

A variant whose `check_command` equals the common one is rejected at upload: both would run
and the score denominator would double. A config stored before this rule existed is not
broken by it — at check time the duplicate is dropped and a warning is logged.

## Adding a New Subject

1. Build a local `<subjectCode>/config.yml` with the full config, plus validate/check scripts for each assignment, laid out as shown above.
2. Zip the directory (`config.yml` must be at the ZIP's root).
3. Upload the ZIP via the teacher portal's "Apply config" form (or `POST /teacher/subjects/apply-config`). This creates the subject, its assignments, and stores the checker code (the ZIP itself) on the server — no restart needed.
4. Enroll students via the teacher portal (use the subject's CSV template).

---

## Updating a Subject

Edit your local copy of `config.yml` (and/or the check scripts), re-zip the whole directory, and upload it again through the same form/endpoint. The service detects the changed content hash, inserts a new config version, and stores the new ZIP — nothing to delete or merge by hand. In-progress submission checks continue using the previous version's stored ZIP; new checks use the updated one. Uploading an identical ZIP again is a no-op.

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
