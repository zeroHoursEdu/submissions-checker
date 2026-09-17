# Squads (paired work) — design

Date: 2026-09-17. Branch: `main` at the time of writing; implementation goes on a
feature branch.

## Goal

Let students on a subject form a **squad** (2..N members, N set per subject in
`config.yml`) and hand in lab work together:

- one ZIP upload by any member counts for the whole squad;
- the defence quiz is split: each member answers a disjoint, near-equal slice of
  one shared draw;
- each member must pass their own slice; the squad's quiz score is the mean of
  the members' slice percentages;
- the final grade is one unified mark, rounded **up**, written to every member;
- until every member has passed, nobody in the squad has a grade.

First consumer: `basicsOfTheDistributedSoftwareDevelopment` (`distributedBasics`,
`review_mode: quiz_then_teacher`, 15 questions, threshold 70 %, 2 attempts) with
`maxAllowedSize: 2`.

## Locked decisions (resolved during brainstorming)

| # | Decision |
|---|----------|
| Q1 | Squad size is a subject-level optional config key `squads.maxAllowedSize`. Students self-join **or** a teacher assigns. Neither side can change a squad after it is locked; fixes go straight to the DB. |
| Q2 | A student may join / be assigned only while they have **zero submissions in the subject**. The squad then covers every assignment of the subject. No retroactive inheritance. |
| Q3 | Quiz pass is **per member**: each slice is scored against `pass_threshold_pct` on its own; a failing member retries their own slice with their own `max_quiz_attempts` counter. Any member exhausting attempts fails the squad submission. |
| Q4 | Join mechanism is **invite from the roster**: creator picks eligible classmates, invitee accepts or declines. Teacher assignment is immediate, no acceptance. |
| Q5 | Submitting is **blocked** while the student has any pending invite (in or out). Cancel or answer the invite first. |
| Model | **Shared submission**: one `submissions` row with `squad_id`; all members resolve it through a single helper. No mirror rows. |

## Data model

Migration `0032_squads`.

```
squads
  id                     BIGINT identity PK
  subject_id             BIGINT FK subjects(id) ON DELETE CASCADE, indexed
  name                   VARCHAR(100) NULL      -- optional, teacher-given; UI falls back to "Сквад #<id>"
  created_by_student_id  BIGINT FK students(id) ON DELETE SET NULL, NULL when teacher-made
  created_by_user_id     BIGINT FK users(id)    ON DELETE SET NULL, NULL when student-made
  locked_at              TIMESTAMPTZ NULL       -- set when size reaches max OR first submission
  created_at / updated_at (TimestampMixin)

squad_members
  squad_id     BIGINT FK squads(id)   ON DELETE CASCADE
  student_id   BIGINT FK students(id) ON DELETE CASCADE
  subject_id   BIGINT FK subjects(id) ON DELETE CASCADE   -- denormalised from squads for the unique index
  joined_at    TIMESTAMPTZ NOT NULL
  PRIMARY KEY (squad_id, student_id)
  UNIQUE (subject_id, student_id)                          -- one squad per student per subject
  CHECK enforced in service: squad_members.subject_id == squads.subject_id

squad_invites
  id                     BIGINT identity PK
  squad_id               BIGINT FK squads(id)   ON DELETE CASCADE, indexed
  invited_student_id     BIGINT FK students(id) ON DELETE CASCADE, indexed
  invited_by_student_id  BIGINT FK students(id) ON DELETE SET NULL
  status                 squad_invite_status ENUM ('PENDING','ACCEPTED','DECLINED','CANCELLED')
  created_at / updated_at
  UNIQUE (squad_id, invited_student_id) WHERE status = 'PENDING'
```

Existing tables:

| Table | Change | Meaning |
|---|---|---|
| `subjects` | `squad_max_size INT NULL` | NULL = squads disabled. Written by config apply. |
| `submissions` | `squad_id BIGINT NULL FK squads ON DELETE SET NULL`, indexed | Set at creation when the submitter is in a locked squad. Never updated afterwards. |
| `quiz_attempts` | `student_id BIGINT NULL FK students ON DELETE RESTRICT`, indexed | Which member took the attempt. NULL only on rows created before this migration (solo). New solo attempts also set it. |
| `students_assignments` | none | `grade` is written to every member's row on finalize. |

`Submission.source_metadata["squad_quiz_draw"]` (JSON, written by the first member
to open the quiz):

```json
{"question_ids": [3, 17, 0, ...], "slices": [[3, 0, ...], [17, ...]], "member_order": [<student_id>, <student_id>]}
```

`QuizAttempt.config_snapshot["squad"]`:

```json
{"member_index": 0, "member_count": 2, "total_questions": 15}
```

`Submission.grade_breakdown["squad"]` (added by `finalize_grade` for squad submissions):

```json
{"unified": true, "members": [{"student_id": 12, "quiz_pct": 86.7}, {"student_id": 15, "quiz_pct": 71.4}]}
```

Enum: `SquadInviteStatus` in `db/models/enums.py`. Models: `db/models/squad.py`
(`Squad`, `SquadMember`, `SquadInvite`).

## Configuration

`config.yml`, subject level, optional:

```yaml
squads:
  maxAllowedSize: 2   # integer 2..6; omit the block to disable squads
```

`ConfigApplyService._validate_squads(new_cfg)`:

- block absent → `subject.squad_max_size = None`;
- block present but not a mapping, or `maxAllowedSize` missing / not an int / outside
  2..6 → `ValueError("squads.maxAllowedSize must be an integer between 2 and 6")`,
  surfaced like every other apply error;
- `_apply_subject_fields` writes the column on every apply (create or update).

Lowering the size, or removing the block, on a later upload never touches existing
squads or their submissions; it only stops new squads from forming (and caps new
ones). Existing locked squads keep working end to end.

The subject repo `basicsOfTheDistributedSoftwareDevelopment/config.yml` gains the block
with `maxAllowedSize: 2` and a Ukrainian comment describing the mechanic.

## Service layer — `services/squads.py`

Single home for every rule. Every function takes an `AsyncSession` and does not commit.

| Function | Behaviour |
|---|---|
| `enabled(subject) -> bool` | `subject.squad_max_size is not None`. |
| `eligibility(db, subject_id, student_id) -> Eligibility` | Returns `OK` or a reason enum: `DISABLED`, `ALREADY_IN_SQUAD`, `HAS_SUBMISSIONS`, `NOT_ENROLLED`, `TEST_STUDENT`. "Has submissions" = any `submissions` row whose `students_assignment` belongs to this student and any assignment of the subject. |
| `eligible_classmates(db, subject_id, student_id) -> list[Student]` | Enrolled REAL students of the subject, excluding self, who are `OK`. Feeds the invite multi-select and the teacher assign form. |
| `create_with_invites(db, subject_id, creator_id, invitee_ids)` | Creator and each invitee must be `OK`; `1 ≤ len(invitees) ≤ max-1`; invitees distinct. Creates squad (creator member) + PENDING invites. Audit `squad_create`. In-app notification to each invitee. |
| `accept_invite(db, invite_id, student_id)` | Invite must be PENDING and addressed to the caller. Re-runs `eligibility` for the invitee **and** checks the squad is still unlocked and below max; on failure the invite becomes CANCELLED and the error names the reason. On success: member row, invite ACCEPTED, every other PENDING invite of this student in the subject → CANCELLED; if size == max → `locked_at = now` and remaining PENDING invites of the squad → CANCELLED. Audit `squad_join`. Notify creator. |
| `decline_invite(db, invite_id, student_id)` | PENDING → DECLINED. Notify creator. |
| `cancel_invite(db, invite_id, student_id)` | Caller must be the creator. PENDING → CANCELLED. If the squad now has one member, no PENDING invites and no submissions, delete the squad row (no lingering solo squads). |
| `teacher_assign(db, subject_id, teacher_user_id, student_ids, name)` | `2 ≤ n ≤ max`; all `OK`; creates squad with all members, `locked_at = now`; cancels the members' PENDING invites (in and out). Audit `squad_assign`. Notify members. |
| `active_squad(db, subject_id, student_id) -> Squad | None` | The student's squad in this subject, **only if locked**. Unlocked squads are UI state, never grading state. |
| `pending_state(db, subject_id, student_id)` | For the student card: outgoing squad + its invites, or incoming invites. |
| `member_sa_ids(db, squad, subjects_assignment_id) -> list[int]` | `students_assignments.id` of every member for that assignment; creates missing rows (same fan-out `_ensure_assignment_rows` does). |
| `resolve_submission_scope(db, sa) -> list[int]` | `[sa.id]` for a solo student; `member_sa_ids(...)` when `active_squad` exists. **Every** "latest submission for this assignment" query goes through this. |
| `latest_submission(db, sa) -> Submission | None` | `max(created_at)` over the scope. Replaces the `max(sa.submissions, ...)` idiom. |
| `lock_on_submit(squad)` | Sets `locked_at` if NULL. Called from `submit_assignment`. |
| `quiz_complete(db, submission) -> bool` | Solo: the passed attempt exists. Squad: every member has an `is_passed` attempt on this submission. |

## Flows

### Formation (student)

1. Student opens the subject page. Card state comes from `enabled` + `eligibility` +
   `pending_state`.
2. `POST /portal/subjects/{id}/squad/create` with `invitee_ids[]` →
   `create_with_invites` → redirect back with flash.
3. Invitee sees the incoming card and an in-app notification →
   `POST /portal/subjects/{id}/squad/invites/{invite_id}/accept|decline`.
4. When the squad reaches `maxAllowedSize` it locks. A creator who changes their mind
   cancels the invite (`.../invites/{id}/cancel`); if that empties the squad the row is
   deleted.

### Formation (teacher)

`POST /teacher/subjects/{id}/squads/assign` with `student_ids[]` and optional `name`
→ `teacher_assign` → redirect to the Операції tab with a flash. Errors (ineligible
student, wrong count) are rendered as a flash naming the student.

### Submission

`submit_assignment`:

1. Any PENDING invite in or out for this student in this subject → 409, flash
   `vocab.squad.submit_blocked_pending`.
2. `scope = resolve_submission_scope(db, sa)`. The existing guards — deadline, "already
   COMPLETED", `max_submissions` — count rows over `students_assignment_id IN scope`
   instead of `== sa.id`.
3. The similarity scan excludes the squad-mates' SA rows (same ZIP by design).
4. `Submission(students_assignment_id=sa.id, squad_id=squad.id)`; `lock_on_submit`.
5. Pipeline unchanged from here: checks, AI review, teacher review all act on the one
   row. `NEW_SUBMISSION` / teacher digest payloads carry `squad_id` so the email and
   board can name the squad.

Reading state — `assignments_list`, `assignment_detail`, `assignment_status`,
`student_summary` — use `latest_submission(db, sa)`. `assignment_detail` also loads the
squad (members, who submitted, each member's quiz state) for the panel.

### Quiz

`start_or_resume_quiz`:

1. `latest_sub = latest_submission(db, sa)`; must be `QUIZ_SENT` as today.
2. Attempt lookup: `submission_id == latest_sub.id AND student_id == me`. In-progress /
   passed / attempts-used are all evaluated per member.
3. Draw:
   - Solo: unchanged (`_build_questions_from_config`), `student_id = me`.
   - Squad, no `squad_quiz_draw` yet: build the full draw of `questions_to_send`
     exactly as today, then `split_draw(question_ids, member_count)` → slices. Save
     into `submission.source_metadata["squad_quiz_draw"]` with `member_order` =
     member student ids sorted ascending. Only the **first** attempt of any member
     writes it (guarded by re-reading inside the same transaction).
   - Squad, draw exists, **first attempt of this member**: `questions_snapshot` = the
     questions of my slice (shuffle order/options per existing flags).
   - Squad, **retry**: fresh random draw of `len(my_slice)` questions from the bank
     (required first, then optional), same as a solo retry produces a new draw. The
     slice size is what stays fixed, not the question ids.
   - `config_snapshot["squad"] = {member_index, member_count, total_questions}`.
     `max_score`, `pass_threshold_pct`, timing, anti-cheat, disputes, proctoring all
     work on the slice unchanged.
4. `split_draw(ids, n)` — pure function in `services/quiz_split.py`: required ids are
   dealt round-robin first, then optional ids round-robin, so slice sizes differ by at
   most one and every id appears in exactly one slice. Unit-tested for n = 1..6.

`_grade_and_finalize` (and `quiz_regrade._advance_submission`):

- on pass: if `quiz_complete(db, submission)` → transition `quiz_passed` /
  `quiz_passed_teacher` (+ `finalize_grade` / teacher notification) as today; else
  leave `QUIZ_SENT`, notify the other members "partner passed their part";
- on fail with attempts exhausted → `quiz_failed` on the submission (whole squad).
  Members who already passed keep their attempt rows; the submission is FAILED;
- `QUIZ_RESULT` payload gains `student_id`; `execute_quiz_result_task` emails that
  student, not the SA owner.

Teacher pages that list attempts per submission (review page, integrity rows,
proctoring) show the member's name from `quiz_attempts.student_id`.

### Grading

`finalize_grade`:

- `quiz_pct` — solo: as today; squad: arithmetic mean of each member's passed attempt
  `score / max_score * 100`.
- `compute_grade(..., round_up=True)` for squad submissions: `math.ceil` instead of
  `round` at the final scaling step. Solo keeps `round`.
- Writes `grade` to **every** SA in `resolve_submission_scope`; writes
  `grade_breakdown` (with the `squad` block) on the submission.
- Because `finalize_grade` runs only after `quiz_complete`, no member has a grade
  until all have passed — the "not enough info" rule falls out of the flow.

Teacher approve/reject, re-run checks, retry AI, bulk actions are unchanged: they
already operate on the submission row. Re-run checks resets nothing squad-related;
`squad_quiz_draw` stays.

## UI

One partial `templates/_squad_badge.html`: pill `👥 <name>` with a `title` listing
members. Rendered next to every unified mark so "shared" reads at a glance.

**Student — subject page (`assignments.html`)**, card above the table, one of:

| State | Content |
|---|---|
| disabled | no card |
| eligible | "Працювати у сквaді": multi-select of `eligible_classmates` (up to max-1) → Запросити |
| incoming invite(s) | "{name} запрошує вас у сквад" → Прийняти / Відхилити |
| outgoing pending | members + "очікує: {names}" + Скасувати, note that submitting is blocked |
| locked | members, badge, no controls |

Grade cell: badge when the row's submission has `squad_id`. Status cell for a squad
row at `QUIZ_SENT` where I already passed: "чекаємо на {names}".

**Student — assignment detail**: squad panel with one row per member — submitted?,
quiz state (не почато / N спроб / ✓ / ✗). Quiz button: "Пройти свою частину тесту
({k} з {total} питань)". Grade block while incomplete: "Оцінка з'явиться, коли всі
учасники сквaду пройдуть тест". When graded: grade + badge + tooltip "середнє по
сквaду, округлено вгору".

**Student — quiz page and result page**: header line "Ваша частина: {k} питань з
{total}; партнер(и): {names}". Result page adds the same "unified mark comes after
everyone passes" note when the squad is incomplete.

**Student — summary**: badge in the grade column for squad rows.

**Teacher — Студенти tab (grid)**: badge in the name cell of squad members. Squad
mates render the shared cell (grade, review score) with their **own** slice pct in
the quiz sub-mark. New `CellStatus` variant `waiting_partner` (amber) for a member who
passed while the submission is still `QUIZ_SENT`.

**Teacher — Операції tab**: block "Сквади" (only when enabled) — list of squads
(name, members, locked / pending, submissions count) and the assign form
(multi-select of eligible students, 2..max, optional name).

**Teacher — assignment board and review page**: badge + members in the submission
row; actions unchanged.

**Notifications (in-app)**: invite received, invite accepted / declined / cancelled,
partner passed their part, grade issued (existing `SUBMISSION_REVIEWED` fans out to
every member).

**i18n**: new `vocab.squad.*` section in `i18n/uk.yml`.

## Error handling

- Every rule violation from `services/squads.py` raises `SquadError(reason)`; routes
  map it to 409 and a flash (`?squad_error=<reason>` on redirect, same pattern as the
  enrolment flash).
- Race: two members open the quiz at once → both compute a draw; the second
  `UPDATE` wins. Guard: `SELECT ... FOR UPDATE` on the submission row before reading
  `squad_quiz_draw`.
- Race: partner submits while an invite is being accepted → `accept_invite` re-checks
  eligibility inside the transaction and cancels the invite.
- Squad member unenrolled by the teacher: `squad_members` row stays (no FK to
  `subjects_students`); the gradebook simply stops listing them. `quiz_complete`
  counts members who are still enrolled. Documented as a known limitation.

## Documentation deliverables

1. `docs/features/squads.md` — the feature sheet requested:
   why the feature exists; what it does (rules list); how to use it (teacher: config
   key, assign form, grid; student: invite / accept, submit, own slice); common flow
   (numbered `quiz_then_teacher` walk-through); tables touched; edge cases table
   (exhausted attempts, teacher reject, dispute regrade, config size lowered, re-run
   checks, unenrolled member).
2. `docs/PLUGIN_AUTHORING.md` — `squads` key under the `config.yml` reference.
3. `docs/feature_catalog.md` — new routes and the squad rule in §3/§4/§5.
4. `CLAUDE.md` map line for `services/squads.py`, `services/quiz_split.py`,
   `api/routes/student_squads.py`.
5. Subject repo `config.yml` block with comment.

## Testing

- **Unit**: `split_draw` (sizes differ ≤ 1, required spread first, every id exactly
  once, n = 1..6); `compute_grade(round_up=True)`; squad `quiz_pct` mean;
  `quiz_complete` predicate over attempt fixtures; `_validate_squads`.
- **Integration** (testcontainers): `services/squads.py` — eligibility matrix, accept
  after partner submitted → CANCELLED, unique (subject, student), lock on full and on
  submit, `cancel_invite` deleting an empty squad, `teacher_assign`, `member_sa_ids`
  creating missing SA rows.
- **Functional** (real app over ASGI):
  - pair flow on `quiz_then_teacher`: A invites B → B accepts → A submits → both see
    `QUIZ_SENT` → A passes slice, grades NULL, B's page says waiting → B passes →
    `AWAITING_TEACHER_REVIEW` → teacher approves → both SA grades equal, breakdown has
    `squad`;
  - submit blocked while an invite is pending; allowed after cancel;
  - B exhausts attempts → submission FAILED, A's page reflects it;
  - solo student on a squads-enabled subject: every existing flow unchanged
    (regression);
  - teacher assign with an ineligible student → flash, no squad.
- **e2e**: one Playwright scenario — invite → accept → badge visible on both portals.
  Skipped if it pushes the suite past budget; functional coverage is the gate.

## Out of scope

Leaving or dissolving a squad, renaming after creation, per-assignment opt-out,
teacher edits of locked squads (DB only), splitting the draw by difficulty, squad
chat, cross-subject squads.
