---
name: student-access-ops
description: Student account tasks on production — create/enrol a student, give login credentials, add missing lab rows, give extra quiz attempts, unstick a quiz. Use for "додай доступ", "згенеруй креди", "докинь спробу", "накинь роботи", "немає кнопки перепройти тест".
---

# Student access ops (prod)

Always: take context from `connect-to-prod-db` (read-only queries), take a backup before any `--write`
(`scripts/ops/run-prod-backup.sh --now`), do the **smallest** change, verify by reading it back, and
ask first when the request is ambiguous about someone's account. `--write` only for the specific change asked.
Env: `PROD_ENV_FILE=/home/vampir/petProjects/submissions-checker/ssh/prod.env` when working in a worktree.

## Add a student and enrol
The teacher gives `email` + "ГРУПА Прізвище Ім'я". The portal route `POST /teacher/students/add`
(`teacher_portal.py::add_student`) creates Student (group created if missing, `full_name = "First Last"`),
user (`username = first.last`, role STUDENT) and credentials. `POST /teacher/subjects/{id}/enroll/{student_id}`
enrols (`subjects_students`) and calls `_ensure_assignment_rows` (one `students_assignments` row per lab).
Read those functions before reproducing them in SQL/Python. After enrol check there are 7 `students_assignments`
rows ("накинь роботи якщо їх немає"): `select subjects_assignment_id from students_assignments where student_id=…`.
Subject 1 = «Основи розробки розподіленого програмного забезпечення мовою C++», 7 labs.

## Credentials
- Normal path: `POST /teacher/students/resend-credentials` rotates the password and e-mails it (OutboxMessage `SEND_CREDENTIALS`, password sealed).
- When the teacher wants the password *shown in chat*: set it directly: `users.password_hash = hash_password(plain)`
  (`submissions_checker.core.security.hash_password`, bcrypt cost 12), set `password_changed_at = now()` so old sessions die;
  generate a random 12+ char password, share `username`, password and site `submisison-checker.duckdns.org` (sic, that is the real domain).
  Run the hash inside the app container (`prod-compose.sh exec -T app python -`), never print stored hashes.
  Treat the shared password as sensitive; tell the teacher the student should change it.

## Extra quiz attempts
Stored per student in `submissions.source_metadata["quiz_extra_attempts"] = {"<student_id>": n}`;
effective cap = `max_quiz_attempts + n` (`services/quiz_grants.py`). Config id 8 has `max_quiz_attempts = 2` for lab5 and lab7.
- The portal button (`POST /teacher/submissions/{id}/grant-quiz-attempt`) only works for students stuck on an exhausted, failed quiz.
- To give attempts to students who still can pass (or already passed and want a retake): edit the JSON for the
  squad's shared submission, one entry per student; keep the structure; do it with `--write` in one statement.
- Since commit `bb0ad13` (branch `javaAndScriptDeveloper/add-student-credentials`; must be merged to `main` and
  rolled out by Watchtower to take effect) a granted attempt also **reopens a quiz the student already passed**
  (retake button appears; failing the retake keeps the earlier pass). Without that commit on prod the button is
  missing for students who passed. Check which version prod runs: `curl https://submisison-checker.duckdns.org/version`.

## Pitfalls
- Squad members each have their own `quiz_attempts`; the squad grade uses the mean of members' best passed quizzes.
- Never change `grade` directly (see `set-student-marks`).
- Don't merge or deploy unless asked: prod deploy = merge to `main`, Watchtower pulls `ghcr.io/zerohoursedu/submissions-checker:main`.
