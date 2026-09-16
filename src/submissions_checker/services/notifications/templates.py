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
        retry_line = (
            f"\n\nYou have {attempts_left} attempt(s) remaining. Re-upload your work to retry."
        )
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


def teacher_digest_template(
    teacher_name: str,
    items: list[tuple[str, str, str]],
    dashboard_url: str,
) -> tuple[str, str]:
    """Return (subject, body) for a coalesced teacher review digest.

    ``items`` is a list of (student_name, assignment_title, review_url). Renders
    1..N pending works as a single email so a whole-group burst is one message.
    """
    count = len(items)
    noun = "submission" if count == 1 else "submissions"
    subject = f"{count} {noun} awaiting your review"

    lines = [
        f"- {student_name} — '{assignment_title}': {review_url}"
        for student_name, assignment_title, review_url in items
    ]
    body = (
        f"Hi {teacher_name},\n\n"
        f"You have {count} {noun} awaiting review:\n\n"
        + "\n".join(lines)
        + f"\n\nOpen your dashboard: {dashboard_url}\n\n"
        f"Best regards,\nSubmissionChecker"
    )
    return subject, body


def password_reset_template(full_name: str, reset_url: str) -> tuple[str, str]:
    """Return (subject, body) for a password reset email."""
    subject = "Reset your SubmissionChecker password"
    body = (
        f"Hi {full_name},\n\n"
        f"You requested a password reset. Click the link below to set a new password "
        f"(valid for 2 hours):\n\n"
        f"{reset_url}\n\n"
        f"If you did not request this, ignore this email.\n\n"
        f"Best regards,\nThe Teaching Team"
    )
    return subject, body


def feedback_request_template(
    full_name: str, subject_name: str, semester_name: str, feedback_url: str
) -> tuple[str, str]:
    """Return (subject, body) for a feedback request notification."""
    subject = f"Share your feedback for '{subject_name}' — {semester_name}"
    body = (
        f"Hi {full_name},\n\n"
        f"Your teacher would like to hear your feedback for the course '{subject_name}' "
        f"({semester_name}).\n\n"
        f"Please take a few minutes to share your thoughts by clicking the link below:\n\n"
        f"{feedback_url}\n\n"
        f"The link is personal and can only be used once.\n\n"
        f"Best regards,\nThe Teaching Team"
    )
    return subject, body


def credentials_template(
    full_name: str, username: str, password: str, login_url: str
) -> tuple[str, str]:
    """Return (subject, body) for a new student account welcome email."""
    subject = "Реєстрація в сервісі перевірки лабораторних робіт"
    body = (
        f"Вітаю, {full_name} !\n\n"
        f"Ваш обліковий запис у сервісі перевірки та оцінювання "
        f"лабораторних робіт створено.\n\n"
        f"Дані для входу:\n"
        f"Логін: {username}\n"
        f"Пароль: {password}\n\n"
        f"Увійти: {login_url}\n\n"
        f"Рекомендуємо змінити пароль одразу після першого входу."
    )
    return subject, body


def quiz_dispute_resolved_template(
    full_name: str,
    assignment_title: str,
    question_text: str,
    decision: str,
    note: str,
    score: int | None,
    max_score: int | None,
    is_passed: bool | None,
    portal_url: str,
) -> tuple[str, str]:
    """Return (subject, body) for the outcome of a reported quiz question.

    Three audiences: the student whose report was accepted, the student whose report was
    rejected, and a classmate who never reported anything but whose result changed because
    someone else's report was upheld.
    """
    if decision == "accept":
        subject = f"Your reported question for '{assignment_title}' was accepted"
        opening = (
            "You reported a question as incorrect, and your teacher agreed. The question "
            "has been credited to everyone who received it."
        )
    elif decision == "reject":
        subject = f"Your reported question for '{assignment_title}' was reviewed"
        opening = (
            "You reported a question as incorrect. After reviewing it, your teacher "
            "decided the question stands as written, so your score is unchanged."
        )
    else:
        subject = f"Your quiz result for '{assignment_title}' was recalculated"
        opening = (
            "A question on your quiz was found to be incorrect and has been credited to "
            "everyone who received it, so your result has been recalculated."
        )

    lines = [f"Hi {full_name},", "", opening, "", f"Question: {question_text}"]
    if note:
        lines += ["", f"Teacher's note: {note}"]
    if score is not None and max_score is not None:
        outcome = ""
        if is_passed is not None:
            outcome = " — passed" if is_passed else " — not passed"
        lines += ["", f"Your new score: {score}/{max_score}{outcome}"]
    lines += ["", f"View your result: {portal_url}", "", "Best regards,", "The Teaching Team"]

    return subject, "\n".join(lines)
