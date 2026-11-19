---
name: prod-grades-queries
description: Ready read-only SQL for the production DB about grades, labs, quizzes, squads and LLM drafts — use when the teacher asks what a student has, who is missing a grade, or before setting marks.
---

# Prod grade queries (read-only)

Run with `PROD_ENV_FILE=… scripts/ops/connect-to-prod-db.sh -c "<one statement>"` (see `connect-to-prod-db`).
Output is personal data: summarise, don't paste whole tables unless the teacher needs the list.
Only `-c` is supported (no `-A/-t`). Wrap the statement in double quotes; Ukrainian text is fine.

Model: `students` → `students_assignments` (sa; one per student per lab; `grade`, `teacher_scores` JSON
`{"report":4,"star":2,"accepted":1}`) → `submissions` (`status`, `squad_id`, `grade_breakdown`).
Squad work: ONE submission, attached to one member's sa; the other members' sa rows have no submission
but share `grade` and `teacher_scores`. `quiz_attempts` is per `student_id` and `submission_id`.
`subjects_assignments`: id 1..7 = ЛР1..ЛР7 of subject 1 (`max_grade`, `config->'requirements'`).
Squads: `squads` + `squad_members`. LLM report drafts: `classroom_student_links` (student_id) →
`classroom_works` (subjects_assignment_id) → `llm_gradings` (`draft->'criteria'`, `approved_at`).

```sql
-- all grades of one subject per student, in lab order ("." = none)
select s.full_name, string_agg(coalesce(sa.grade::text,'.'), ',' order by sa.subjects_assignment_id)
from students s join students_assignments sa on sa.student_id=s.id and sa.subjects_assignment_id<=7
group by s.id, s.full_name having count(sa.grade)>0 order by 1;

-- state of named students: grade, scores, submissions
select s.id, s.full_name, sa.subjects_assignment_id a, sa.grade, sa.teacher_scores::text ts,
  (select string_agg(sub.id||':'||sub.status||'/sq'||coalesce(sub.squad_id::text,'-'), ',')
   from submissions sub where sub.students_assignment_id=sa.id) subs
from students s join students_assignments sa on sa.student_id=s.id
where s.full_name ~* '(Козлов|Сокуренко)' and sa.subjects_assignment_id<=7 order by 2,3;

-- quiz result per submission and student (exact; do not join by squad_id, it mixes labs)
select qa.submission_id, qa.student_id, string_agg(qa.score||'/'||qa.max_score||case when qa.is_passed then 'P' else 'f' end, ' ' order by qa.id)
from quiz_attempts qa where qa.submission_id in (51,57) group by 1,2;

-- LLM report draft points per student and lab
select w.subjects_assignment_id a, l.student_id, g.id, g.approved_at is not null appr,
  (select string_agg(k||'='||(v->>'points'), ',') from jsonb_each(g.draft->'criteria') e(k,v)) pts
from classroom_student_links l join classroom_works w on w.link_id=l.id
join llm_gradings g on g.classroom_work_id=w.id where l.student_id in (9,14);

-- squad parity: MUST return 0 rows (both members same grade per lab)
select sq.id squad, sa.subjects_assignment_id a, count(distinct coalesce(sa.grade,-1)) g
from squads sq join squad_members sm on sm.squad_id=sq.id
join students_assignments sa on sa.student_id=sm.student_id
group by 1,2 having count(distinct coalesce(sa.grade,-1))>1;

-- who is in which squad
select sm.squad_id, string_agg(s.full_name, ' + ') from squad_members sm
join students s on s.id=sm.student_id group by 1 order by 1;

-- per-lab max grade and plan
select id, max_grade, config->'requirements' from subjects_assignments where subject_id=1 order by id;

-- recent notifications queue (did the e-mails go out?)
select event_type, state, count(*) from outbox_messages where created_at > now() - interval '3 hours' group by 1,2;
```

E-mail delivery proof: `scripts/ops/prod-compose.sh logs --since 60m app | grep submission_reviewed_email_sent`.
