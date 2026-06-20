# Student Guide — Using the Submissions Checker

A plain-language, step-by-step guide to everything you can do as a **student** in this
system, from your very first login to seeing your final grade. No technical background
is assumed.

> This is the end-user companion to the shorter, route-level
> [student-journey.md](student-journey.md) and the teacher-side
> [student-registration.md](student-registration.md). Where the two overlap, this guide
> is the friendly walkthrough; the others are the quick reference.

---

## Before you start: how accounts work

You do **not** sign yourself up. Your teacher creates your account for you (usually by
uploading a class list). Once that happens, the system emails you your login details:

- a **username**, usually in the form `firstname.lastname` (for example `ivan.petrenko`),
- a **temporary password** (a random string — change it after you first log in),
- a **link to the login page**.

If you never received the email, ask your teacher to check your account's email status —
the message may simply be queued, or your address may have been mistyped.

A few things that are true throughout the system, and matter to you:

- **You only ever see your own data.** Every page checks that the submission, quiz, grade,
  or assignment you are opening belongs to you. You cannot open another student's
  submission or quiz, even with a direct link — the system returns a "not found" or
  "forbidden" error.
- **You can only see subjects you are enrolled in.** Opening a subject you are not enrolled
  in returns "Not enrolled in this subject".
- **There is no self-enrolment.** Your teacher enrols you into a subject; you cannot add
  yourself.

---

## Feature catalogue (quick reference)

| What you can do | Where (page / link) | Who can use it | What you get |
|---|---|---|---|
| Log in | `/auth/login` | You, with your account | A signed-in session (lasts 8 hours) |
| Reset a forgotten password | `/auth/forgot-password` → emailed link | Anyone with an account | A new password you choose |
| Choose interface language | language switch (`/set-language`) | Anyone | English or Ukrainian UI, remembered for a year |
| Give proctoring consent | `/portal/consent` | You (once) | Permission to take proctored quizzes |
| See all your subjects | `/portal` | You | A grid of enrolled subjects with progress |
| See your overall standing | `/portal/summary` | You | Average grade, upcoming and overdue work |
| See a subject's assignments | `/portal/subjects/{subject}` | Enrolled students | List with deadlines and statuses |
| Open one assignment | `/portal/subjects/{subject}/assignments/{id}` | Owner only | Brief, files, history, submit button |
| Submit work (ZIP) | "Submit" on the assignment page | Owner only | Your work is queued for checking |
| Watch checking progress | the assignment page (refresh) | Owner only | Live status of your submission |
| Take a quiz | `/portal/.../quiz` → `/portal/quiz/{id}` | Owner, after consent | A graded, possibly proctored quiz |
| See quiz results | `/portal/quiz/{id}/result` | Owner only | Your score and (maybe) correct answers |
| See in-app notifications | `/notifications` | Any signed-in user | Your message inbox |
| Choose which emails you get | `/portal/notification-preferences` | You | Per-event email on/off |
| Give course feedback | `/feedback/{token}` (emailed link) | Anyone with the link | Anonymous-style course feedback |

---

## 1. Logging in

1. Open the login page (`/auth/login`).
2. Type your **username** and **password** and submit.
3. If they match, you are taken to your portal home (`/portal`). Your session is kept in a
   secure browser cookie and lasts **8 hours**, after which you log in again.
4. If they do not match, you see *"Invalid username or password"* and can try again.

**Logging out:** use the log-out action; it clears your session immediately.

Every successful login is recorded. This is normal — teachers use login records to spot
suspicious patterns (for example, the same account logging in from many places at once).

### Forgot your password?

1. Go to **Forgot password** (`/auth/forgot-password`) and enter your username.
2. The page always says a link has been sent (it will not confirm whether the username
   exists — this protects accounts from being guessed). If the username is real **and** has
   an email on file, a reset link is emailed to you.
3. Open the link (`/auth/reset-password?token=…`). It is **single-use** and expires after a
   couple of hours.
4. Choose a new password (at least **8 characters**) and confirm it. If the two entries do
   not match, or the password is too short, you are asked to try again.
5. On success you can log in with the new password.

---

## 2. Choosing your language (optional)

The interface is available in **English** and **Ukrainian**. Use the language switcher to
change it. Your choice is stored in your browser for a year, so you only set it once.

---

## 3. Proctoring consent (one-time)

The first time you open your portal, you are asked to read and accept a **recording /
proctoring notice** (`/portal/consent`). This explains that, for quizzes, the system may
monitor for cheating and — if your teacher enables it — capture webcam frames.

- You must accept this notice **before** you can take any quiz.
- You accept it **once**; the system records the date and time and never asks again.
- If you try to reach the portal home or a quiz before consenting, you are automatically
  redirected back to the consent page.

---

## 4. Finding your subjects and assignments

### Your subjects (`/portal`)

The portal home shows a **grid of the subjects you are enrolled in**. Each subject card
shows its name, an optional picture, and your **progress** — how many of its assignments
you have already had graded out of the total.

### Your overall standing (`/portal/summary`)

The summary page gives a cross-subject dashboard:

- your **average grade** across everything graded so far,
- how many assignments are graded vs. the total,
- **upcoming deadlines** (work due within the next 7 days that you have not finished),
- **overdue** work (past deadline and not yet graded).

### A subject's assignments (`/portal/subjects/{subject}`)

Opening a subject lists its assignments, ordered by deadline (assignments with no deadline
come last). For each you see the title, the deadline, your grade (if any), the grading
range, and the status of your most recent submission.

> You can only open subjects you are enrolled in. A direct link to any other subject is
> rejected.

### One assignment in detail (`/portal/subjects/{subject}/assignments/{id}`)

This is the main working page for a task. It shows:

- the **full description / brief**,
- any **attached files** the teacher provided (PDFs, starter code, instructions),
- your **submission history** and the **current status**,
- how many **quiz attempts** you have used and how many you are allowed (if the assignment
  uses a quiz),
- the **action buttons** that make sense right now (submit, take quiz, view result).

> This page is strictly yours: it only loads if the assignment belongs to your account.

---

## 5. Submitting your work (ZIP upload)

You submit work as a single **ZIP file**.

**How to submit:**

1. On the assignment detail page, choose your `.zip` file and submit it.
2. The system saves it and immediately queues it for automatic checking. The page returns
   you to the assignment, where you can watch the status.

**Rules the system enforces at the moment you submit** (if any of these block you, you get a
clear message and the upload is refused):

- **File type:** only `.zip` files are accepted.
- **File size:** the ZIP must be **50 MB or smaller**.
- **Deadline:** if the deadline has passed and the assignment's policy is to *block* late
  work, the submission is refused. (Some assignments allow late submissions — that depends
  on the teacher's setting.)
- **Already passed:** once a submission for this assignment has been marked **completed**,
  you cannot submit again — it is done.
- **Attempt limit:** some assignments cap the number of submissions (`max_submissions`).
  Once you hit the cap, further uploads are refused.

**Similarity / plagiarism check:** when you upload, the system compares your ZIP against
**other students'** submissions for the same assignment and records a similarity score on
your submission. This score is for the teacher; high similarity may be reviewed.

After a successful upload your submission starts as **pending** and is picked up by a
background worker, which runs the actual checks.

---

## 6. Watching your submission get checked

Refresh the assignment page to follow your submission through these stages. The exact path
depends on how the teacher configured the assignment (its *review mode*):

1. **Validating** — an optional structural check (is the ZIP well-formed?). If it fails, the
   status becomes **validation failed** and you can fix and resubmit.
2. **Testing** — the subject's automated tests run in a secure, isolated sandbox (no
   internet, locked down, time- and memory-limited). A **score** is computed and compared to
   the assignment's pass mark.
3. **What happens next depends on the review mode:**
   - *Tests only* → **completed** straight away once tests pass.
   - *Tests then AI review* → an automated review runs, then **completed**.
   - *Tests then teacher review* → status becomes **awaiting teacher review**; your teacher
     grades it by hand.
   - *Tests then AI then teacher* → AI review, then teacher review.
   - *Tests then quiz* → status becomes **quiz sent**; you must now take a quiz (Section 7).

You can see the per-test results on the assignment page. **Which** test names and details
are shown to you is up to the teacher's configuration — some subjects reveal everything,
others show only pass/fail. If a check explains *why* it failed, that reason is shown too.

> If a submission ends up **failed**, read the message and (attempts permitting) fix your
> work and submit again.

---

## 7. Taking a quiz (when an assignment requires one)

Some assignments require you to pass a short quiz after your code passes its tests.

**Starting a quiz:**

1. On the assignment page (status **quiz sent**), choose to start the quiz.
2. You must have given **proctoring consent** (Section 3) — if not, you are sent to the
   consent page first.
3. The system creates an attempt and shows you the quiz (`/portal/quiz/{id}`).

**How attempts work:**

- If you already have a quiz **in progress**, you are returned to it (you cannot start a
  fresh one to dodge a timer).
- If you have **already passed**, you are sent to your result instead.
- Assignments may cap the number of attempts (`max_quiz_attempts`). Once you have used them
  all without passing, the submission is marked **failed** and you are shown your last
  result.

**The quiz itself:**

- Questions are **snapshotted** when you start, so they do not change mid-attempt. They may
  be **shuffled**, and answer options may be shuffled too.
- Question types you may see: **single choice**, **multiple choice**, **ordering**,
  **true/false**, and **short answer**.
- If the quiz has a **time limit**, a countdown is shown. When time runs out the attempt is
  auto-submitted and marked **timed out**.

**Anti-cheat monitoring (if enabled):** while the quiz is open, the page watches for events
such as switching tabs, leaving the window, resizing, copy attempts, certain keyboard
shortcuts, right-clicking, or leaving full-screen. Depending on the teacher's rules, an
event can:

- show you a **warning**,
- be silently **flagged** for the teacher,
- **reduce your remaining time** as a penalty, or
- **fail the attempt outright** once a threshold is reached.

**Webcam proctoring (if enabled):** if your teacher turned on camera proctoring, the page
may capture a webcam frame when a monitored event occurs. These frames are stored as
evidence for the teacher. (If storage is not set up on the server, capture is skipped
silently and never blocks your quiz.)

**Submitting the quiz:**

- Submit when you are done. Your answers are graded automatically (except short-answer text,
  which is recorded but not auto-scored).
- The attempt's final status is **completed**, **timed out**, or **violation fail**.
- You **pass** if your score reaches the configured threshold (commonly 60%). Passing marks
  the whole submission **completed**.

**Seeing your result (`/portal/quiz/{id}/result`):** you see your score, the maximum score,
whether you passed, and a per-question breakdown. **Correct answers are only revealed if the
teacher enabled that option** for the quiz.

---

## 8. Seeing results and grades

- A final grade appears on the subject and assignment pages once your submission reaches
  **completed**, or once a teacher assigns a grade by hand.
- Your portal home and summary update your progress counts as work gets graded.

---

## 9. Notifications

### Your inbox (`/notifications`)

In-app notifications collect important updates — for example, that a submission finished
checking, or that a feedback request is waiting. You can:

- view your latest notifications (most recent first),
- **mark one as read**,
- **mark all as read**,
- see your **unread count** (used for the badge in the interface).

> You only ever see notifications addressed to your own account.

### Email preferences (`/portal/notification-preferences`)

You control which events email you. You can toggle **email** on or off for each case:

- **Submission Checked** — when your submission finishes checking.
- **Feedback Request** — when a teacher asks you for course feedback.

By default these are **on**; toggling sets your personal preference. In-app notifications are
always available regardless of the email setting.

---

## 10. Giving course feedback

At the end of a course (or whenever the teacher asks), you may receive a **feedback link** by
email.

1. Open the link (`/feedback/{token}`). It is **personal and single-use** — you do **not**
   need to be logged in to use it.
2. Fill in the form: a **1–5 rating** plus three short text answers — **what went well**,
   **what went badly**, and **what you would change**.
3. Submit. You are taken to a thank-you page (`/feedback/{token}/thanks`).

Notes:

- The rating must be between **1 and 5**; anything else is rejected.
- The link works **once**. If you open it again after submitting, you see a "already
  submitted" page. An unknown or broken link shows a "not found" page.

---

## Permissions and privacy — the short version

- **Your account is a student account.** Teacher-only and admin-only pages are blocked for
  you (you would get a "student access required"/"forbidden" error).
- **Ownership is checked everywhere.** Submissions, quiz attempts, quiz results, assignment
  details, and notifications all verify they belong to you before loading.
- **Subjects are scoped to enrolment.** You can only browse subjects you are enrolled in.
- **Quizzes require consent first**, and the questions are locked in at the moment you start.
- **Feedback links are anonymous-style and single-use** — they are tied to a request, not to
  a login session.

If you ever see a "not found" or "forbidden" message on a link you expected to work, it
usually means the item is not yours, you are not enrolled, or (for quizzes/feedback) the
attempt or link has already been used.
