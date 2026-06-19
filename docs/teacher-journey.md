# Teacher Journey

What a teacher can do in submissions-checker, end to end. Each step lists the
page/route behind it. Teacher routes use the `/teacher` prefix; analytics live under
`/teacher/analytics`. A `TEACHER`-role account is created by an admin
(see [the admin section below](#admin-only-account-management)).

> Authorization: a teacher only sees and acts on subjects they own
> (`Subject.owner_id`); `require_subject_access` enforces this. ADMIN may access all.

## 1. Log in

- `GET /auth/login` → `POST /auth/login`. JWT in an HTTP-only cookie (8h).
- Password reset via `/auth/forgot-password` and `/auth/reset-password`.

## 2. Dashboard

- `GET /teacher` — overview of the subjects the teacher owns, with quick links into
  each subject.

## 3. Create / update a subject (config ZIP)

- `POST /teacher/subjects/apply-config` — upload a subject config ZIP containing
  `config.yml` plus per-assignment check scripts and optional content files/images.
- The config is parsed, content-hashed, and stored as a **versioned**
  `SubjectPluginConfig` (deduplicated — re-uploading identical content is a no-op).
  The subject and its assignments are upserted by code. See
  [PLUGIN_AUTHORING.md](PLUGIN_AUTHORING.md) for the `config.yml` schema (assignments,
  deadlines, grade ranges, `review_mode`, late policy, `max_submissions`, variants,
  sandbox limits, quiz + anti-cheat).
- `POST /teacher/subjects/{subject_id}/delete` — mark a subject DELETED.
- `GET /teacher/subjects/{subject_id}` — subject detail: enrolled students,
  assignments, and the active plugin-config version.

## 4. Enroll students

- **Bulk CSV import:** `GET /teacher/subjects/{subject_id}/students/template.csv`
  (download template), then `POST /teacher/subjects/{subject_id}/students/import`.
  A global importer also exists at `POST /teacher/students/import` with a sample at
  `GET /teacher/students/sample.csv`. CSV import creates `Student` + linked `User`
  accounts and enrolls them.
- **Single add:** `GET /teacher/students/add` → `POST /teacher/students/add`.
- **Enroll / unenroll existing students:**
  `POST /teacher/subjects/{subject_id}/enroll/{student_id}` and
  `…/unenroll/{student_id}`.
- Browse all students: `GET /teacher/students`.

## 5. Pilot the subject as a test student

- `POST /teacher/subjects/{subject_id}/test-student` — provision a TEST student
  account for the subject (kept separate from real students; excluded from analytics).
- `POST /teacher/subjects/{subject_id}/test-student/enter` — impersonate that test
  student to walk the full student journey (submit, take the quiz) before going live.

## 6. Review and grade submissions

- `GET /teacher/subjects/{subject_id}/assignments/{sa_id}` — assignment view with the
  submission stream for enrolled students.
- `GET /teacher/submissions/{submission_id}/review` — review a single submission:
  automated test results, AI review (if `review_mode` includes it), and submitted code.
- `POST /teacher/submissions/{submission_id}/review` — act on it: approve (→ COMPLETED),
  reject (→ FAILED), or send a quiz (→ QUIZ_SENT). This is where manual grades are set
  for `tests_then_teacher` / `…_then_teacher` review modes.
- Already-graded work is skipped on re-runs.

## 7. Export results

- `GET /teacher/subjects/{subject_id}/export.csv` — download grades for the subject.

## 8. Analytics and fraud detection

- `GET /teacher/analytics` — dashboard: enrollment, average grade, pass rate,
  per-assignment difficulty ranking, grade-distribution histogram.
- `GET /teacher/analytics/fraud` — risk-scored flags:
  - late first login (<24h before deadline) with a high grade,
  - very few logins (<3) with a high average grade,
  - 3+ submissions on the same calendar day,
  - quizzes failed after exhausting all attempts.
- `GET /teacher/analytics/students/{student_id}` — per-student profile: grade timeline
  and login activity.

> Note: analytics currently aggregate across subjects and are gated accordingly;
> per-teacher scoping is a tracked follow-up (see the missing-features backlog).

## 9. Collect course feedback

- `POST /teacher/subjects/{subject_id}/feedback/request` — create a per-semester
  feedback request; tokenized, single-use links are distributed to enrolled students.
- `GET /teacher/subjects/{subject_id}/feedback` — view collected responses
  (1–5 ratings + free-text).
- `GET /teacher/subjects/{subject_id}/feedback/export.csv` — export responses.

## 10. Notifications

- `GET /notifications` — in-app notifications (new submissions, reviews, etc.), with
  read / read-all and an unread count, shared with the student notification UI.

---

## Admin-only account management

Admins (`/admin`) sit above teachers:

- `GET /admin` — system dashboard (user counts, outbox status, recent audit log).
- `GET /admin/users` — list users; `POST /admin/users/{user_id}/toggle-active` —
  activate/deactivate.
- `GET /admin/teachers/create` → `POST /admin/teachers/create` — create teacher
  accounts.
- `GET /admin/audit` — immutable audit log of administrative actions.
