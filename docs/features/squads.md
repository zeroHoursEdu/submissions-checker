# Squads (paired work)

## Why

Some labs are meant to be done in pairs: the work is one artifact, the defence quiz still
has to prove each partner understands it, and the grade should be one shared mark rather
than two independent ones that can drift apart. Squads let a teacher opt a subject into
that shape — one ZIP upload for the pair, one shared defence (split so both are actually
tested), one unified grade — without changing anything for subjects that don't use it.

## What it does

- **Opt-in per subject.** A subject has squads only when its config declares
  `squads.maxAllowedSize`. Every other subject behaves exactly as before.
- **Join before any submission.** A student can create or accept a squad only while they
  have zero submissions in the subject. Once they've submitted solo, squads are closed to
  them for that subject.
- **Two ways to form one.** A student invites classmates from the roster (invite →
  accept/decline); or a teacher assigns a group directly from the Операції tab (no
  acceptance needed, immediate).
- **Locked at max size or first upload.** A squad locks (`locked_at` set) the moment it
  reaches `maxAllowedSize`, or the moment any member submits — whichever comes first.
  Nothing about the membership can change after that; DB-only fixes described in
  [Edge cases](#edge-cases) are the only way out.
- **One upload for all.** Any member's ZIP counts for the whole squad — one shared
  `submissions` row, referenced by every member's `students_assignments` record for that
  assignment.
- **Quiz draw split into slices, required spread first.** The first member to open the
  quiz triggers one shared draw of the full question set; `split_draw` deals it into
  near-equal slices (round-robin, required questions first) so no member's slice is all
  optional filler.
- **Per-member threshold and attempts.** Each member takes their own slice against the
  subject's `pass_threshold_pct` and `max_quiz_attempts`, independently of their partner.
- **Exhausted member fails the squad.** If any member uses up their attempts without
  passing their slice, the whole submission goes `FAILED` — not just that member.
- **Grade = mean of slices, rounded up, written to all.** `finalize_grade` averages each
  passed member's slice percentage, rounds the final grade **up** (`math.ceil`, not
  nearest), and writes the same `grade` to every member's `students_assignments` row.
- **No grade until all pass.** `finalize_grade` only runs once `quiz_complete` is true for
  every currently-enrolled member, so "nobody has a grade yet" falls straight out of the
  normal flow — there's no separate check for it.

## How to use — teacher

Enable squads for a subject by adding a `squads:` block to its `config.yml` and
re-uploading the config:

```yaml
squads:
  maxAllowedSize: 2        # 2..6
```

Omit the block (or remove it on a later upload) to keep every student solo — see
[Edge cases](#edge-cases) for what happens to squads that already exist.

Once enabled, the subject page's **Операції** tab gains a "Сквади" block: a table of
existing squads (name, members, locked/pending) and an assign form —
pick 2..`maxAllowedSize` eligible students from a multi-select, optional squad name,
"Сформувати сквад". Ineligible picks (already in a squad, already submitted, not
enrolled, duplicate) come back as a flash naming the student and no squad is created.

On the **Студенти** grid, squad members carry a `👥 <name>` badge next to their name; a
squad-mate's grade and review-score cells are filled in from the shared submission, but
the **quiz sub-mark still shows that member's own slice percentage**, not their
partner's. A member who has passed their own slice while the submission is still
`QUIZ_SENT` (waiting on a partner) renders as an amber ⏳ cell instead of a score.

The **review page** and the **assignment board** show the same `👥` badge and list every
member next to the submission; approve/reject/re-run/retry-AI act on the one shared row
exactly as for a solo submission.

## How to use — student

The subject's assignment list shows a squad card above the assignment table, one of:

| Card state | What the student sees |
|---|---|
| Sizes disabled | no card |
| Eligible (no submissions, not already in a squad) | "Сквад" heading + hint text, multi-select of eligible classmates (up to max−1) → "Запросити у сквад" |
| Incoming invite(s) | "{name} запрошує вас у сквад" → "Прийняти" / "Відхилити" |
| Outgoing, still pending | current members + "Очікуємо відповіді: {names}" + "Скасувати запрошення" (creator only — see [Edge cases](#edge-cases)); a note that submitting is blocked until every invite is resolved |
| Locked | members + `👥` badge, no controls |

Submitting is refused (409, flash) while the student has **any** pending invite, in or
out — cancel or answer it first.

Once locked, whoever uploads submits for the whole squad. The assignment detail page
shows a squad panel — one row per member: submitted?, quiz state (не почато / N спроб /
✓ / ✗). The quiz button reads **"Пройти свою частину тесту (k з total питань)"** — only
that member's slice. While the squad isn't complete, the grade block says "Оцінка
з'явиться, коли всі учасники сквaду пройдуть тест"; once graded it shows the mark, the
badge, and — as a note under the panel, not a tooltip — "Спільна оцінка сквaду — середнє
по учасниках, округлене вгору."

The quiz-taking pages (question view and the per-question stepper) carry a header line:
"Ваша частина: k з total питань · Партнер(и): {names}". The result page does **not**
repeat that header — while the squad is still incomplete it shows only the same "Оцінка
з'явиться…" note as the assignment detail page. The student summary page shows the same
badge in the grade column for squad rows.

## Common flow

A worked example on a `quiz_then_teacher` assignment (no check script — upload,
then quiz, then teacher approval), squad of two (A, B):

1. A opens the subject page (squads enabled, A has no submissions yet), picks B from the
   eligible-classmates multi-select, clicks "Запросити у сквад" →
   `POST /portal/subjects/{id}/squad/create` creates the squad (A as member) and a
   `PENDING` invite to B.
2. B sees the incoming-invite card and an in-app notification, clicks "Прийняти" →
   `POST /portal/subjects/{id}/squad/invites/{invite_id}/accept`. The squad reaches
   `maxAllowedSize` (2) and locks immediately.
3. A uploads the ZIP. It becomes one `submissions` row with `squad_id` set, shared by
   both A's and B's `students_assignments` rows for this assignment.
4. Both A and B see the assignment move to `QUIZ_SENT`. The first of them to open the
   quiz triggers the shared draw and its split into two slices; each answers only their
   own slice.
5. A passes their slice first. `quiz_complete` is still false (B hasn't passed), so the
   submission stays `QUIZ_SENT` — no grade yet. A's page shows "Оцінка з'явиться, коли
   всі учасники сквaду пройдуть тест"; B gets a notification that a partner passed their
   part.
6. B passes their slice. `quiz_complete` is now true for both — the submission
   transitions to `AWAITING_TEACHER_REVIEW`.
7. The teacher opens the review page (sees the `👥` badge and both names), approves the
   submission → `COMPLETED`. `finalize_grade` averages A's and B's slice percentages,
   rounds up, and writes the same grade to both A's and B's `students_assignments` rows,
   with `grade_breakdown["squad"]` recording each member's slice percentage.

## Tables touched

| Table / column | Role |
|---|---|
| `subjects.squad_max_size` | `NULL` = squads disabled for the subject; set by config apply from `squads.maxAllowedSize`. |
| `squads` | One row per formed squad: subject, optional name, creator (student or teacher user), `locked_at`. |
| `squad_members` | One row per (squad, student); unique on `(subject_id, student_id)` — a student is in at most one squad per subject. |
| `squad_invites` | Invite lifecycle: `PENDING → ACCEPTED / DECLINED / CANCELLED`; unique on `(squad_id, invited_student_id)` while `PENDING`. |
| `submissions.squad_id` | Set at creation when the submitter is in a locked squad; never updated afterwards. |
| `submissions.source_metadata.squad_quiz_draw` | `{question_ids, slices, member_order}` — the one shared draw, written once by whichever member opens the quiz first. |
| `quiz_attempts.student_id` | Which member took the attempt; solo attempts pre-migration have `NULL` here. |
| `quiz_attempts.config_snapshot.squad` | `{member_index, member_count, total_questions}` for the "your part" banner. |
| `students_assignments.grade` fan-out | `finalize_grade` writes the same rounded-up grade to every member's row for the assignment. |
| `submissions.grade_breakdown.squad` | `{unified: true, squad_id, members: [{student_id, quiz_pct}, ...]}`. |

## Edge cases

| Case | What happens |
|---|---|
| A member exhausts quiz attempts without passing | Submission goes `FAILED` (whole squad); members who already passed keep their attempt rows. |
| Teacher rejects the submission | Same as solo: `FAILED`, both members notified. |
| A dispute (reported-question) regrade flips an attempt to passing | Waits on completeness like any other pass — the submission only advances once `quiz_complete` is true for every enrolled member. |
| Config's `squads.maxAllowedSize` is lowered, or the `squads:` block is removed | Never touches existing squads or their submissions; only stops new squads from forming (and caps new ones at the new size). Existing locked squads keep working end to end. |
| Teacher re-runs checks on a squad submission | Resets the submission status as usual; `squad_quiz_draw` in `source_metadata` is untouched, so a re-sent quiz reuses the same draw and slices. |
| A member is unenrolled from the subject after joining a squad | Their `squad_members` row stays (no FK to enrollment); `quiz_complete` only requires currently-enrolled members to have passed, so the gradebook simply stops listing the unenrolled member. |
| Панель "На перевірці" count (teacher's subject Панель tab) | Known limitation — counts only the uploader, not every squad member. |
| Grid waiting (⏳) tooltip | Known limitation — shows a placeholder, not the actual partner names. |
| Declining or cancelling an invite | Not written to the audit log (unlike `squad_create` / `squad_join` / `squad_assign`) — known limitation. |
| Need to undo a locked squad, or drop a member entirely | DB only, by design (no UI/API undoes a lock): `DELETE FROM squad_members WHERE squad_id = … AND student_id = …` to drop a member, or `UPDATE squads SET locked_at = NULL WHERE id = …` to unlock one for further changes. |
| CSV grade export | Shows the unified grade for every member, but the Status / Submitted-at columns are populated only on the uploader's row — squad-mates' rows show those blank. |
| Deadline reminders | Still go to a squad-mate whose partner already uploaded on the squad's behalf — the reminder job is per-enrollment, not squad-aware. |

## Routes

| Route | Who | Audited as |
|---|---|---|
| `POST /portal/subjects/{subject_id}/squad/create` | STUDENT, enrolled | `squad_create` |
| `POST /portal/subjects/{subject_id}/squad/invites/{invite_id}/accept` | STUDENT, enrolled, invitee | `squad_join` |
| `POST /portal/subjects/{subject_id}/squad/invites/{invite_id}/decline` | STUDENT, enrolled, invitee | not audited |
| `POST /portal/subjects/{subject_id}/squad/invites/{invite_id}/cancel` | STUDENT, enrolled, **squad creator only** | not audited |
| `POST /teacher/subjects/{id}/squads/assign` | Owner / ADMIN | `squad_assign` |

Only the squad's creator sees and can use the Cancel-invite button — a non-creator
member has no control over invites they didn't send.

## Implementation notes

Migration `0032_squads`. Service layer: `src/submissions_checker/services/squads.py`
(every rule; nothing here commits) and
`src/submissions_checker/services/quiz_split.py` (`split_draw`, pure and DB-free).
Student routes: `src/submissions_checker/api/routes/student_squads.py`. The design doc
used function names `resolve_submission_scope`; the shipped code instead splits that
seam into `scope_sa_ids` (the list of `students_assignments.id` in scope),
`latest_submission_for` (looked up by student/subject/assignment) and
`latest_submission` (looked up from a `StudentAssignment` row) — same contract, finer
names. A repeated student id in an invite or teacher-assign form is refused outright
(`SquadError("duplicate")`), never silently deduplicated. `accept_invite` takes
`SELECT ... FOR UPDATE` on the squad row before checking capacity, so two concurrent
accepts can't both observe "not yet full" and overshoot `squad_max_size`. Declining an
invite applies the same "delete if left empty" rule as cancelling the last invite does
(one member, no pending invites, unlocked) — so a creator whose only invitee declines
can invite someone else instead of being stuck with a dead squad.
