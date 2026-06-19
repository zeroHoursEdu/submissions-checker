# Statuses

> **DEPRECATED / HISTORICAL.** The `pending → cloning → reviewing → completed` flow and
> the `PULL`/`REVIEW`/`NOTIFY` outbox events below described the retired GitHub-PR ingest
> pipeline. Submissions are now ZIP-uploaded and driven by the state machine in
> `core/state_machine.py` (`VALIDATING → TESTING → {COMPLETED | AWAITING_AI_REVIEW |
> AWAITING_TEACHER_REVIEW | QUIZ_SENT | …}`). See [student-journey.md](student-journey.md)
> for the current lifecycle. This file is kept only for historical context.

## Submission status

Tracks the lifecycle of a single student submission.

| Status | Set by | Meaning |
|---|---|---|
| `pending` | Default on record creation | Submission row created, waiting for the PULL job to start cloning. |
| `cloning` | PULL job | Repository is being (or has been) cloned to the local workspace. A REVIEW outbox message has been queued. |
| `reviewing` | REVIEW job | AI code review is in progress. |
| `completed` | REVIEW job | AI review finished; quiz questions stored; pipeline continues asynchronously via GENERATE_QUIZ → NOTIFY → NOTIFY_QUIZ_RESULT. |
| `failed` | Any job on unrecoverable error | Processing stopped. The associated outbox message will have an `error` state with a description. |

The happy-path flow is:

```
pending → cloning → reviewing → completed
```

---

## Outbox message state

Tracks the processing state of a single entry in the `outbox_messages` table.

| State | Meaning |
|---|---|
| `pending` | Message created, not yet picked up by the processor. |
| `finished` | Handler completed successfully. The message will not be processed again. |
| `error` | Handler threw an exception. The message will be retried on the next processor cycle, up to `outbox_max_retries` times. The `error_message` column holds the last exception string and `retry_count` tracks how many attempts have been made. After reaching `outbox_max_retries`, the message stays in `error` and requires manual intervention. |

---

## Outbox event types

| Event type | Handler | Description |
|---|---|---|
| `PULL` | `execute_pull_task` | Clone the student fork and create a Submission record. |
| `REVIEW` | `execute_review_task` | Run AI code review; generate quiz questions via OpenAI. |
| `GENERATE_QUIZ` | `execute_generate_quiz_task` | Send questions to Google Apps Script; create a Google Form. |
| `NOTIFY` | `execute_notify_task` | Post the quiz link as a comment on the student's GitHub PR. |
| `NOTIFY_QUIZ_RESULT` | `execute_notify_quiz_result_task` | Send pass/fail email to the student via Brevo. |
