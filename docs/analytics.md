# Teacher Analytics

The Analytics section gives teachers a data-driven view of student performance and potential academic integrity issues.

## Accessing Analytics

From the Teacher Dashboard, click the **Analytics** button (top-right, next to "Register students"). The section has three pages accessible from each other via breadcrumbs and in-page links.

---

## Overview Dashboard (`/teacher/analytics`)

A platform-wide snapshot with four headline numbers:

| Card | What it shows |
|------|---------------|
| Students | Unique students enrolled in at least one subject |
| Subjects | Total subjects in the system |
| Avg Grade | Mean grade across all graded assignments (ungraded assignments are excluded) |
| Pass Rate | % of graded assignments where the student's grade ≥ the assignment's minimum grade |

**Grade Distribution chart** — bar chart showing how grades are spread across 10-point buckets (0–9, 10–19, …, 100). Useful for spotting bimodal distributions or grade inflation.

**Performance by Subject table** — enrollment count, graded count, average grade, and total assignments per subject. Click the subject name to navigate to the full subject detail page.

**Assignment Difficulty table** — all assignments ranked from hardest to easiest (lowest average grade first). Shows enrolled vs. submitted counts and a visual difficulty bar: red = hard, amber = medium, green = easy.

---

## Student Profile (`/teacher/analytics/students/{id}`)

A cross-subject view of a single student. Reached by clicking a student name anywhere in the Fraud Detection page.

- **Header card** — full name, group, email, GitHub handle (if set), total login count, first and last login date.
- **Subject profile cards** — one card per enrolled subject showing graded/total assignments, average grade with colour coding, and a progress bar.
- **Grade Timeline chart** — line chart showing the student's grades in chronological order (ordered by assignment deadline), overlaid with the minimum and maximum grade lines for context.
- **All Assignments table** — every assignment across all subjects with deadline, grade, maximum grade, and a pass/fail indicator (✓ / ✗ / –).

---

## Fraud Detection (`/teacher/analytics/fraud`)

Surfaces three types of suspicious patterns. **These are signals, not proof** — they require teacher judgement before any action is taken.

### Late First-Login + High Grade

Students whose **first-ever login** happened within 24 hours before an assignment deadline, and who received a grade ≥ 80% of the maximum for that assignment.

**Pattern:** No prior engagement with the platform, then a sudden high-quality submission right at the deadline.

### Few Logins + High Grade

Students with **fewer than 3 total logins** whose average grade across graded assignments is ≥ 75.

**Pattern:** Almost no observable system usage, yet consistently strong results.

### Single-Day Submission Burst

Students who submitted **3 or more assignments all on the same calendar day**.

**Pattern:** Work submitted in a single batch rather than spread across the course, suggesting it may not have been developed incrementally.

### Risk Score

Each student receives a composite risk score based on how many flags they trigger:

| Flag | Points |
|------|--------|
| Late first-login + high grade | +3 |
| Few logins + high grade | +2 |
| Single-day burst | +2 |

Risk levels: **0** = none, **1–2** = Low (amber), **3–4** = Medium (orange), **5+** = High (red).

The **Login Activity Overview** table at the bottom lists all students sorted by login count ascending, so students who have never logged in appear at the top. Click any student name to open their individual profile page.

---

## Performance Notes

Six composite database indexes are added by migration `0005` to keep analytics queries fast even with large datasets. All aggregations are performed in a single SQL query per metric — no N+1 queries are issued. Chart.js (≈200 KB gzipped) is only loaded on analytics pages and does not affect the rest of the application.
