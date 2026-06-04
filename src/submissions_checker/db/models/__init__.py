"""Database models and enums."""

from submissions_checker.db.models.audit_log import AuditLog
from submissions_checker.db.models.enums import (
    OutboxEventType,
    OutboxMessageState,
    QuizAttemptStatus,
    QuizQuestionType,
    SubmissionSourceType,
    SubmissionStatus,
    UserRole,
)
from submissions_checker.db.models.group import Group
from submissions_checker.db.models.notification import Notification
from submissions_checker.db.models.outbox import OutboxMessage
from submissions_checker.db.models.password_reset import PasswordResetToken
from submissions_checker.db.models.quiz_template import QuizAnswer, QuizAttempt
from submissions_checker.db.models.student import Student
from submissions_checker.db.models.student_assignment import StudentAssignment
from submissions_checker.db.models.subject import Subject, SubjectsStudents
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.db.models.submission import Submission
from submissions_checker.db.models.user import User
from submissions_checker.db.models.user_login import UserLogin

__all__ = [
    "AuditLog",
    "Notification",
    "OutboxEventType",
    "OutboxMessageState",
    "PasswordResetToken",
    "QuizAttemptStatus",
    "QuizAnswer",
    "QuizAttempt",
    "QuizQuestionType",
    "SubmissionSourceType",
    "SubmissionStatus",
    "UserRole",
    "Group",
    "OutboxMessage",
    "Student",
    "StudentAssignment",
    "Subject",
    "SubjectPluginConfig",
    "SubjectsAssignment",
    "SubjectsStudents",
    "Submission",
    "User",
    "UserLogin",
]
