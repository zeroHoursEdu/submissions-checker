## 1. Strip content-mutating affordances from the teacher UI

- [x] 1.1 Remove the Analytics link from `templates/teacher_dashboard.html:52`, leaving the Apply
      config form, the Students link and the subject cards untouched.
- [x] 1.2 Remove the subject edit, export CSV and delete blocks from `templates/teacher_subject.html`
      (lines 41, 45, 50), keeping the feedback link and the feedback-request form beside them.
- [x] 1.3 Remove the create-assignment link from `templates/teacher_subject.html:122`; the
      assignment list itself and its per-assignment links stay.
- [x] 1.4 Remove the quiz editor, assignment edit and export CSV links from
      `templates/teacher_assignment.html` (lines 69, 76, 80), keeping the submission table and its
      review links.
- [x] 1.5 Confirm the test-student panel (`templates/teacher_subject.html:187-196`) still renders
      and both its forms still post, since it is explicitly retained.
- [x] 1.6 Grep the templates for any remaining reference to the removed routes
      (`/edit`, `/export.csv`, `/delete`, `/assignments/create`, `/quiz`) and remove stragglers;
      leave the route handlers in `teacher_portal.py` in place.

## 2. Rework the per-subject import endpoint

- [x] 2.1 Rewrite `import_subject_students` (`teacher_portal.py:591`) to parse the new header:
      `email` required, `variant` optional. Keep the existing 1 MB and UTF-8 guards; return 422
      naming `email` when the column is absent, before any row is processed.
- [x] 2.2 Drop the student/group/user-creation branch and the `SEND_CREDENTIALS` outbox write from
      this handler — it enrols only. Match each row by trimmed, lower-cased `email` against
      `students.email`.
- [x] 2.3 For each matched student, insert `subjects_students` when absent, then ensure a
      `students_assignments` row exists for every `subjects_assignments` row of the subject,
      reusing the fan-out logic in `enroll_student` (`teacher_portal.py:1102`) rather than
      duplicating it — extract it into a shared helper if that is cleaner.
- [x] 2.4 Apply the row's `variant` to every one of those `students_assignments` rows when the cell
      is non-empty; leave stored values untouched when it is empty or the column is absent.
- [x] 2.5 Accumulate three counts (newly enrolled, already enrolled, rejected) and a list of
      `(line_number, reason)` pairs, where `line_number` counts the header as line 1 and `reason`
      is one of the closed set `unknown` / `empty`.
- [x] 2.6 Redirect to the subject page with the counts as integers and the rejections as one
      URL-encoded parameter of `line:reason` pairs, capped at 20 entries plus an overflow count,
      following the `apply_error` round-trip pattern at `teacher_portal.py:106-110`.

## 3. Generate the example CSV from the subject's config

- [x] 3.1 Rewrite `download_subject_enrollment_template` (`teacher_portal.py:540`) to emit the
      header `email,variant` and stop deriving `variant_<code>` columns from assignments.
- [x] 3.2 Load the subject's highest-version `SubjectPluginConfig` using the same
      `ORDER BY version DESC LIMIT 1` shape as `_fetch_latest_config` (`check_tasks.py:211`); handle
      a subject with no config at all without raising.
- [x] 3.3 Collect the union of `assignments.<code>.variants` keys across every assignment in that
      config, emitting each key verbatim, sorted numerically when all keys parse as integers and
      lexicographically otherwise.
- [x] 3.4 Emit one row per variant with a placeholder address on the `example.invalid` domain; when
      the subject declares no variants, emit the header plus two placeholder rows with an empty
      variant cell.

## 4. Add the enrolment panel to the subject page

- [x] 4.1 Extend the `teacher_subject` route (`teacher_portal.py:277`) to read the enrolment counts
      and the rejection parameter from the query string and pass them to the template, alongside
      the existing `feedback_sent` / `test_student` flashes.
- [x] 4.2 Add an enrolment panel to `templates/teacher_subject.html` in the space the removed
      buttons vacate: a link to the template CSV, a file input restricted to `.csv`, and a submit
      button posting multipart to `/teacher/subjects/{{ subject.id }}/students/import`.
- [x] 4.3 Render the result summary above the panel — enrolled / already enrolled / rejected counts
      — and list rejected rows as line number plus a translated reason, with the overflow count when
      the cap was hit.
- [x] 4.4 Add the new vocabulary keys to `i18n/uk.yml` under the `teacher` section, including one
      string per rejection reason; leave the now-unused keys behind the removed buttons in place.

## 5. Tests

- [x] 5.1 Functional test in `tests/functional/test_teacher_portal.py`: the subject page contains no
      link to the edit, export, delete or create-assignment routes, and the assignment page contains
      no link to the quiz, edit or export routes.
- [x] 5.2 Functional test: the teacher dashboard contains no link to `/teacher/analytics`.
- [x] 5.3 Functional test: uploading `email,variant` enrols an existing student, creates one
      `students_assignments` row per assignment, and writes the variant to all of them.
- [x] 5.4 Functional test: an unknown e-mail is rejected with its line number while the other rows
      still enrol, and no student, user or outbox row is created for it.
- [x] 5.5 Functional test: re-uploading the same file creates no duplicate rows, and a row with an
      empty `variant` leaves the previously stored variant intact.
- [x] 5.6 Functional test: a CSV without an `email` column returns 422 and enrols nobody.
- [x] 5.7 Test for the template download: variants come from the config and merge across
      assignments; a subject with no variants yields the header plus two empty-variant placeholder
      rows.

## 6. Documentation and verification

- [x] 6.1 Correct `docs/teacher_journey_guide.md` and `docs/feature_catalog.md` where they describe
      the removed buttons, and document the enrolment CSV flow.
- [x] 6.2 Update the dead-quiz-editor entry at `docs/known_bugs.md:166` to record that the link is
      gone and only the orphaned template remains.
- [x] 6.3 Run `uv run --frozen pytest`, `ruff check`, `ruff format --check` and `mypy`; the repo is
      clean at HEAD, so anything failing belongs to this change.
- [x] 6.4 Drive it in a browser: enrol a real student into distributedBasics from a downloaded
      template, confirm the student sees the subject in their portal and every lab appears.
      *Not run locally.* Shipped to production via the CI/CD pipeline (commit 3e9c876) and
      signed off by the maintainer there; no local browser pass was performed.
