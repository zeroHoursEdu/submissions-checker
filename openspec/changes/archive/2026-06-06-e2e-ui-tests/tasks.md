## 1. Infrastructure & Dependencies

- [x] 1.1 Add `playwright`, `pytest-playwright`, `pytest-bdd`, `psycopg2-binary` to `pyproject.toml` dev/e2e dependency group
- [x] 1.2 Create `docker-compose.e2e.yml` with isolated Postgres (port 5433) and app (port 8001) services
- [x] 1.3 Add `make e2e` target and `make e2e-headed` target to `Makefile` (start stack, run tests, stop stack)
- [x] 1.4 Add "Running E2E tests" section to `README.md` with all options (headed, tags, single scenario, env vars)

## 2. Test Directory Scaffold

- [x] 2.1 Create `tests/e2e/` directory with `conftest.py`, `__init__.py`
- [x] 2.2 Create `tests/e2e/features/` directory for `.feature` files
- [x] 2.3 Create `tests/e2e/steps/` directory for step definition modules
- [x] 2.4 Create `tests/e2e/fixtures/` directory for static test assets (sample ZIP config, sample CSVs)
- [x] 2.5 Create `tests/e2e/pages/` directory for page-object helpers

## 3. Fixtures & Shared State

- [x] 3.1 Implement `teacher_account` session-scoped fixture in `conftest.py` — directly inserts a teacher user into the E2E DB via `psycopg2` and yields credentials
- [x] 3.2 Implement `student_credentials` function-scoped fixture that captures username/password from enrollment flow and makes them available to subsequent steps
- [x] 3.3 Implement `app_url` fixture returning the E2E app base URL (default `http://localhost:8001`)
- [x] 3.4 Create a valid sample subject config ZIP at `tests/e2e/fixtures/sample_subject.zip`
- [x] 3.5 Create a valid sample student CSV at `tests/e2e/fixtures/students.csv`
- [x] 3.6 Create a sample passing submission ZIP at `tests/e2e/fixtures/submission_pass.zip`
- [x] 3.7 Create a sample failing submission ZIP at `tests/e2e/fixtures/submission_fail.zip`

## 4. Page Object Helpers

- [x] 4.1 Create `pages/login_page.py` with `LoginPage` class (fill credentials, submit, assert dashboard)
- [x] 4.2 Create `pages/teacher_dashboard.py` with `TeacherDashboard` class (upload ZIP config, list subjects, open subject)
- [x] 4.3 Create `pages/subject_page.py` with `SubjectPage` class (upload CSV, list enrolled students, open submissions)
- [x] 4.4 Create `pages/student_portal.py` with `StudentPortal` class (list subjects, open assignment, upload submission, check status)
- [x] 4.5 Create `pages/analytics_page.py` with `AnalyticsPage` class (navigate, assert stats visible)
- [x] 4.6 Create `pages/notifications_page.py` with `NotificationsPage` class (toggle channel preferences)
- [x] 4.7 Create `pages/feedback_page.py` with `FeedbackPage` class (request feedback, open token URL, submit response)
- [x] 4.8 Create `pages/quiz_page.py` with `QuizPage` class (start quiz, submit answers, read result)

## 5. Feature Files (Gherkin)

- [x] 5.1 Write `features/teacher_auth.feature` — teacher login happy path and wrong-password sad path
- [x] 5.2 Write `features/subject_management.feature` — ZIP upload creates subject; invalid ZIP shows error
- [x] 5.3 Write `features/student_enrollment.feature` — CSV upload enrolls students; credentials extracted
- [x] 5.4 Write `features/student_auth.feature` — student logs in with generated credentials
- [x] 5.5 Write `features/student_submission.feature` — fail submission → resubmit → pass; blocked after pass
- [x] 5.6 Write `features/analytics.feature` — teacher views analytics page with stats
- [x] 5.7 Write `features/notifications.feature` — student toggles email channel on/off
- [x] 5.8 Write `features/feedback.feature` — teacher requests feedback; student responds via token URL
- [x] 5.9 Write `features/quiz.feature` — student starts quiz, fails, retries, passes

## 6. Step Definitions

- [x] 6.1 Implement `steps/auth_steps.py` — login/logout steps for teacher and student
- [x] 6.2 Implement `steps/subject_steps.py` — subject creation, ZIP upload, subject listing steps
- [x] 6.3 Implement `steps/enrollment_steps.py` — CSV upload, credential extraction, enrolled count steps
- [x] 6.4 Implement `steps/submission_steps.py` — file upload, status assertion, resubmission steps
- [x] 6.5 Implement `steps/analytics_steps.py` — navigation and stats visibility steps
- [x] 6.6 Implement `steps/notification_steps.py` — preference toggle and persistence steps
- [x] 6.7 Implement `steps/feedback_steps.py` — feedback request, token URL navigation, response steps
- [x] 6.8 Implement `steps/quiz_steps.py` — quiz start, answer submission, result assertion steps

## 7. Run Tests & Fix Until Green

- [x] 7.1 Install Playwright browsers (`playwright install chromium`)
- [x] 7.2 Start E2E stack with `docker-compose.e2e.yml` and run full suite; record failures
- [x] 7.3 Fix any app UI issues or step mismatches revealed by test failures
- [x] 7.4 Confirm all scenarios pass in headless mode
- [x] 7.5 Confirm headed mode works for debugging (`make e2e-headed`)
