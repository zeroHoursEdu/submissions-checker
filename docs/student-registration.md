# Student Registration Guide

This guide explains how to register students in EduTrack and how the system manages their accounts.

---

## Overview

Instead of creating accounts one by one, teachers register students in bulk via a CSV file. The workflow is designed around a shared spreadsheet:

1. Teacher downloads a **blank CSV template**.
2. Teacher shares it with students in a group chat or email.
3. Students fill in their details and return the file.
4. Teacher uploads the completed CSV — EduTrack creates accounts and sends each student their login credentials automatically.

---

## Step-by-step

### 1. Download the CSV template

Navigate to **Dashboard → Students** (`/teacher/students`) and click **Download sample CSV**. You will receive a file named `students.csv` with the following columns:

| Column | Description | Example |
|---|---|---|
| `student_group` | Group / cohort name | `IT-21` |
| `student_name` | Student's first name | `Ivan` |
| `student_surname` | Student's last name | `Petrenko` |
| `email` | Student's email address | `ivan@example.com` |

**Example file:**
```
student_group,student_name,student_surname,email
IT-21,Ivan,Petrenko,ivan@university.edu
IT-21,Olena,Kovalenko,olena@university.edu
CS-22,Mykola,Shevchenko,mykola@university.edu
```

### 2. Share with students

Send the blank template to your students via your group chat (Telegram, Slack, Viber, etc.) or by email. Ask each student to fill in **exactly one row** with their details.

> **Tip:** Make sure students use their real email addresses — credentials will be sent there automatically.

### 3. Collect and upload

Once students have filled in the file and returned it to you:

1. Go to **Dashboard → Students**.
2. Click **Choose file** and select the completed CSV.
3. Click **Import students**.

EduTrack will:
- Create a new group if the group name does not exist yet.
- Create a student profile and login account for each row.
- Generate a unique username (`firstname.lastname`) and a random password.
- Queue a **credentials email** to each student with their username, password, and a link to the login page.

The page will show a confirmation banner: *"N new students registered, M already had an account and were skipped."*

---

## What happens after import

### Account creation

Each student receives:
- A **username** in the format `firstname.lastname` (e.g., `ivan.petrenko`). If a collision occurs (two students with the same name), a numeric suffix is added (`ivan.petrenko_2`).
- A **randomly generated password** (12 characters, URL-safe). The student should change it after first login.

### Credentials email

An email is sent to the student's address containing:
- Their username and temporary password.
- A direct link to the login page.

> **Note:** Email delivery requires an email provider to be configured in the application settings (`RESEND_API_KEY`, `BREVO_API_KEY`, or SMTP). If no provider is configured, the email is queued and will be sent as soon as a provider is set up — no students are lost.

---

## Tracking status

The **Registered students** table on the Students page shows the status of each account:

| Column | What it means |
|---|---|
| **Email status** | Whether the credentials email was delivered. `Sent` = delivered; `Pending` = queued (email provider may not be set up yet); `Error` = delivery failed (check provider settings). |
| **First login** | The date and time when the student first logged in to the portal. `Never` means they have not logged in yet. |

---

## Frequently asked questions

**Can I re-upload the same CSV?**
Yes. Students who already have an account (matched by email address) are silently skipped — no duplicate accounts are created. The import summary tells you how many were skipped.

**What if a student's email address changes?**
Accounts are linked to email at the time of registration. To update an email you will need to change it directly in the database or via a future admin UI.

**Can a student be in multiple groups?**
No. A student profile belongs to exactly one group. If a student appears in two different groups in the CSV, only the first occurrence is used (the second is skipped because the email already exists).

**What if the email status shows "Error"?**
The credentials email failed to send. This usually means the email provider is misconfigured or unreachable. Once the issue is fixed and the provider is reachable, the outbox processor will retry automatically (up to 5 attempts).

**Can I add students one at a time?**
Currently the only supported method is CSV import. Single-student registration may be added in a future version.

**Where can I find the student's temporary password after import?**
For security, the password is not displayed in the UI after import. It was sent to the student's email. If the student did not receive it (check **Email status**), you can ask a system administrator to reset the password directly in the database.
