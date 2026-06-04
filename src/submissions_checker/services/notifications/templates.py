"""Email templates for notifications."""


def submission_reviewed_template(
    full_name: str,
    assignment_title: str,
    action: str,
    reason: str,
    portal_url: str,
) -> tuple[str, str]:
    """Return (subject, body) for a reviewed submission notification."""
    verb = "approved" if action == "approve" else "rejected"
    subject = f"Your submission for '{assignment_title}' was {verb}"
    reason_line = f"\n\nFeedback: {reason}" if reason and action == "reject" else ""
    body = (
        f"Hi {full_name},\n\n"
        f"Your submission for assignment '{assignment_title}' has been {verb} by your teacher."
        f"{reason_line}\n\n"
        f"View your submission: {portal_url}\n\n"
        f"Best regards,\nThe Teaching Team"
    )
    return subject, body


def quiz_result_template(
    full_name: str,
    assignment_title: str,
    score: int,
    max_score: int,
    is_passed: bool,
    attempts_left: int | None,
    portal_url: str,
) -> tuple[str, str]:
    """Return (subject, body) for a quiz result notification."""
    verb = "passed" if is_passed else "did not pass"
    subject = f"Quiz result for '{assignment_title}'"
    retry_line = ""
    if not is_passed and attempts_left and attempts_left > 0:
        retry_line = f"\n\nYou have {attempts_left} attempt(s) remaining. Re-upload your work to retry."
    elif not is_passed:
        retry_line = "\n\nAll attempts have been used."
    body = (
        f"Hi {full_name},\n\n"
        f"You {verb} the quiz for '{assignment_title}'.\n\n"
        f"Score: {score}/{max_score}"
        f"{retry_line}\n\n"
        f"View your result: {portal_url}\n\n"
        f"Best regards,\nThe Teaching Team"
    )
    return subject, body


def deadline_reminder_template(
    full_name: str,
    assignment_title: str,
    subject_name: str,
    deadline_str: str,
    portal_url: str,
) -> tuple[str, str]:
    """Return (subject, body) for an upcoming deadline reminder."""
    subject = f"Reminder: '{assignment_title}' deadline tomorrow"
    body = (
        f"Hi {full_name},\n\n"
        f"This is a reminder that the assignment '{assignment_title}' "
        f"for '{subject_name}' is due on {deadline_str}.\n\n"
        f"Submit your work here: {portal_url}\n\n"
        f"Best regards,\nThe Teaching Team"
    )
    return subject, body


def new_submission_template(
    teacher_name: str,
    student_name: str,
    assignment_title: str,
    review_url: str,
) -> tuple[str, str]:
    """Return (subject, body) to notify a teacher of a new submission awaiting review."""
    subject = f"New submission: '{assignment_title}' from {student_name}"
    body = (
        f"Hi {teacher_name},\n\n"
        f"{student_name} has submitted their work for '{assignment_title}' "
        f"and it is awaiting your review.\n\n"
        f"Review it here: {review_url}\n\n"
        f"Best regards,\nEduTrack"
    )
    return subject, body


def password_reset_template(full_name: str, reset_url: str) -> tuple[str, str]:
    """Return (subject, body) for a password reset email."""
    subject = "Reset your EduTrack password"
    body = (
        f"Hi {full_name},\n\n"
        f"You requested a password reset. Click the link below to set a new password "
        f"(valid for 2 hours):\n\n"
        f"{reset_url}\n\n"
        f"If you did not request this, ignore this email.\n\n"
        f"Best regards,\nThe Teaching Team"
    )
    return subject, body


def passed_template(github_username: str, score: int, max_score: int, lab_id: int) -> tuple[str, str]:
    """Return (subject, body) for a passing quiz result."""
    subject = f"Congratulations! You passed Lab {lab_id} Quiz"
    body = (
        f"Hi @{github_username},\n\n"
        f"Great news — you passed the Lab {lab_id} quiz!\n\n"
        f"Your score: {score}/{max_score}\n\n"
        f"Your submission is now complete. Well done!\n\n"
        f"Best regards,\nThe Teaching Team"
    )
    return subject, body


def failed_template(github_username: str, score: int, max_score: int, lab_id: int) -> tuple[str, str]:
    """Return (subject, body) for a failing quiz result."""
    subject = f"Lab {lab_id} Quiz Result — Please Resubmit"
    body = (
        f"Hi @{github_username},\n\n"
        f"Unfortunately, you did not pass the Lab {lab_id} quiz.\n\n"
        f"Your score: {score}/{max_score}\n\n"
        f"To try again, push a new commit to your PR branch — this will trigger a fresh\n"
        f"AI review and generate a new quiz for you.\n\n"
        f"Good luck!\n\n"
        f"Best regards,\nThe Teaching Team"
    )
    return subject, body


def credentials_template(full_name: str, username: str, password: str, login_url: str) -> tuple[str, str]:
    """Return (subject, body) for a new student account welcome email."""
    subject = "Your account credentials for EduTrack"
    body = (
        f"Hi {full_name},\n\n"
        f"Your account has been created. Here are your login details:\n\n"
        f"  Username: {username}\n"
        f"  Password: {password}\n\n"
        f"Log in here: {login_url}\n\n"
        f"We recommend changing your password after your first login.\n\n"
        f"Best regards,\nThe Teaching Team"
    )
    return subject, body
