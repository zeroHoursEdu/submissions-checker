"""Database enums — all values stored as UPPERCASE_WITH_UNDERSCORE in PostgreSQL."""

import enum


class SubmissionStatus(enum.StrEnum):
    PENDING = "PENDING"
    VALIDATING = "VALIDATING"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    TESTING = "TESTING"
    TEST_FAILED = "TEST_FAILED"
    AWAITING_AI_REVIEW = "AWAITING_AI_REVIEW"
    AI_REVIEWING = "AI_REVIEWING"
    AI_REVIEW_FAILED = "AI_REVIEW_FAILED"
    AWAITING_TEACHER_REVIEW = "AWAITING_TEACHER_REVIEW"
    QUIZ_SENT = "QUIZ_SENT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    def __str__(self) -> str:
        return self.value


class SubmissionSourceType(enum.StrEnum):
    ZIP_UPLOAD = "ZIP_UPLOAD"

    def __str__(self) -> str:
        return self.value


class OutboxMessageState(enum.StrEnum):
    PENDING = "PENDING"
    FINISHED = "FINISHED"
    ERROR = "ERROR"

    def __str__(self) -> str:
        return self.value


class OutboxEventType(enum.StrEnum):
    SEND_CREDENTIALS = "SEND_CREDENTIALS"
    SUBMISSION_REVIEWED = "SUBMISSION_REVIEWED"
    QUIZ_RESULT = "QUIZ_RESULT"
    DEADLINE_REMINDER = "DEADLINE_REMINDER"
    NEW_SUBMISSION = "NEW_SUBMISSION"
    RUN_CHECKS = "RUN_CHECKS"
    RUN_AI_REVIEW = "RUN_AI_REVIEW"
    FEEDBACK_REQUEST_SENT = "FEEDBACK_REQUEST_SENT"
    QUIZ_DISPUTE_RESOLVED = "QUIZ_DISPUTE_RESOLVED"

    def __str__(self) -> str:
        return self.value


class UserRole(enum.StrEnum):
    TEACHER = "TEACHER"
    STUDENT = "STUDENT"
    ADMIN = "ADMIN"

    def __str__(self) -> str:
        return self.value


class QuizQuestionType(enum.StrEnum):
    SINGLE_CHOICE = "SINGLE_CHOICE"
    MULTIPLE_CHOICE = "MULTIPLE_CHOICE"
    ORDERING = "ORDERING"
    TRUE_FALSE = "TRUE_FALSE"

    def __str__(self) -> str:
        return self.value


class QuizAttemptStatus(enum.StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    TIMED_OUT = "TIMED_OUT"
    VIOLATION_FAIL = "VIOLATION_FAIL"

    def __str__(self) -> str:
        return self.value


class QuizDisputeStatus(enum.StrEnum):
    """Lifecycle of a student's report that a quiz question is wrong or unanswerable."""

    OPEN = "OPEN"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"

    def __str__(self) -> str:
        return self.value


class NotificationCase(enum.StrEnum):
    SUBMISSION_CHECKED = "SUBMISSION_CHECKED"
    FEEDBACK_REQUEST = "FEEDBACK_REQUEST"

    def __str__(self) -> str:
        return self.value


class NotificationMethod(enum.StrEnum):
    EMAIL = "EMAIL"

    def __str__(self) -> str:
        return self.value


class SubjectStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    DELETED = "DELETED"

    def __str__(self) -> str:
        return self.value


class EntityType(enum.StrEnum):
    REAL = "REAL"
    TEST = "TEST"

    def __str__(self) -> str:
        return self.value
