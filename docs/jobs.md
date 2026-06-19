# Jobs

> **DEPRECATED / HISTORICAL.** The GitHub-PR + Google-Forms pipeline described below
> (`GitHub webhook → PULL → REVIEW → GENERATE_QUIZ → NOTIFY → NOTIFY_QUIZ_RESULT`) has
> been **retired**. Submissions are now ingested **only via ZIP upload** through the
> student portal, checked by the plugin pipeline (`RUN_CHECKS` → `check_tasks`), with
> optional `RUN_AI_REVIEW` / teacher review / in-app quiz. See
> [student-journey.md](student-journey.md) and [statuses.md](statuses.md) for the
> current flow. This file is kept only for historical context.

The system processes student submissions through a pipeline of job types, all driven by the transactional outbox pattern. A background scheduler polls the `outbox_messages` table every 10 seconds and dispatches each pending message to the appropriate handler.

## How the outbox works

1. An event is written to the `outbox_messages` table (alongside any business data, in the same DB transaction).
2. The scheduler acquires a PostgreSQL advisory lock so only one processor runs at a time across all instances.
3. Pending and retryable messages are fetched in creation order, up to the configured batch size.
4. Each message is dispatched to its handler and awaited — not fire-and-forget — so that any follow-on writes (new outbox messages, updated records) are committed atomically with the original message being marked `finished`.
5. On failure the message is marked `error` and retried on the next cycle, up to `outbox_max_retries`.

---

## Full pipeline

```
GitHub webhook
     ↓
   PULL  — clone fork, create Submission record
     ↓
  REVIEW  — AI generates 10 quiz questions from student code
     ↓
GENERATE_QUIZ  — sends questions to Google Apps Script, creates Google Form
     ↓
  NOTIFY  — posts quiz link as a comment on the student's GitHub PR
     ↓
        [student submits the quiz]
     ↓
NOTIFY_QUIZ_RESULT  — sends pass/fail email to the student via Brevo
```

---

## PULL

**Trigger:** GitHub webhook (`pull_request` event, actions `opened` or `synchronize`).

**Handler:** `workers/tasks/pull_tasks.py::execute_pull_task`

**What it does:**

1. Reads the fork repository information from the outbox payload.
2. Shallow-clones the fork branch to `workspace_dir/<fork_owner>/<fork_repo>` on disk. If a clone already exists it is removed first (handles PR updates).
3. Creates or updates a `Submission` record with the clone path, sets status to `cloning`.
4. Creates a `REVIEW` outbox message carrying only the `submission_id`.

**Payload fields:**

| Field | Description |
|---|---|
| `pr_number` | GitHub PR number |
| `fork_clone_url` | HTTPS clone URL of the fork |
| `fork_full_name` | e.g. `student/repo-name` |
| `head_ref` | Branch name |
| `head_sha` | Commit SHA |
| `base_full_name` | Assignment parent repo, e.g. `instructor/repo-name` |
| `action` | `opened` or `synchronize` |

---

## REVIEW

**Trigger:** Created automatically by the PULL job.

**Handler:** `workers/tasks/review_tasks.py::execute_review_task`

**What it does:**

1. Fetches the `Submission` record by `submission_id`.
2. Reads the student's source files and README from the cloned repository path.
3. Fetches lecture theory for the relevant lab from the `lecture_knowledge` table (RAG).
4. Sends a prompt to OpenAI asking it to generate 10 multiple-choice questions at the intersection of the theory and the student's code.
5. Stores the questions JSON in `submission.ai_review`, sets status to `completed`.
6. Creates a `GENERATE_QUIZ` outbox message with the questions.

**Payload fields:**

| Field | Description |
|---|---|
| `submission_id` | Internal `Submission` table PK |

---

## GENERATE_QUIZ

**Trigger:** Created automatically by the REVIEW job.

**Handler:** `workers/tasks/generate_quiz_tasks.py::execute_generate_quiz_task`

**What it does:**

1. Reads `ai_review` questions from the payload (no extra DB fetch needed).
2. POSTs the questions to the Google Apps Script endpoint (`GOOGLE_SCRIPT_URL`), including the callback URL for quiz results (`BASE_URL/webhooks/quiz-submission?submission_id=N`).
3. Receives the created Google Form URL from the Apps Script response.
4. Saves the URL to `submission.quiz_url`.
5. Creates a `NOTIFY` outbox message with the form URL.

**Payload fields:**

| Field | Description |
|---|---|
| `submission_id` | Internal `Submission` table PK |
| `ai_review` | Dict with `questions` array from the REVIEW step |

---

## NOTIFY

**Trigger:** Created automatically by the GENERATE_QUIZ job.

**Handler:** `workers/tasks/notify_tasks.py::execute_notify_task`

**What it does:**

1. Reads `form_url` from the payload.
2. Fetches the `Submission` record to get the GitHub username, repo, and PR number.
3. Posts a comment on the student's GitHub PR with the quiz link.

**Payload fields:**

| Field | Description |
|---|---|
| `submission_id` | Internal `Submission` table PK |
| `form_url` | Google Form URL for the student's quiz |

---

## NOTIFY_QUIZ_RESULT

**Trigger:** Created automatically by the `POST /webhooks/quiz-submission` endpoint when Google Apps Script calls back with the student's quiz score.

**Handler:** `workers/tasks/notify_quiz_result_tasks.py::execute_notify_quiz_result_task`

**What it does:**

1. Reads score and student email from the payload.
2. Fetches the `Submission` record to determine which lab and the student's GitHub username.
3. Sends a pass or fail email to the student via Brevo.
   - **Passed** (score ≥ `QUIZ_PASS_THRESHOLD`): congratulations + score.
   - **Failed**: failure notice + instructions to push a new commit and re-trigger the pipeline.

**Protection:** If a submission already has a passing score in the database, the quiz-submission webhook rejects the update — the score cannot be overwritten once passed.

**Payload fields:**

| Field | Description |
|---|---|
| `submission_id` | Internal `Submission` table PK |
| `student_email` | Student's email address (from the Google Form response) |
| `score` | Student's quiz score |
| `max_score` | Maximum possible score |
