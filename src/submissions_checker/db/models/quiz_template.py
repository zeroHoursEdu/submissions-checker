"""Quiz template, questions, attempts, and answers models."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from submissions_checker.db.models.base import Base, TimestampMixin
from submissions_checker.db.models.enums import QuizAttemptStatus, QuizQuestionType

if TYPE_CHECKING:
    from submissions_checker.db.models.submission import Submission
    from submissions_checker.db.models.subjects_assignment import SubjectsAssignment


class QuizTemplate(Base, TimestampMixin):
    __tablename__ = "quiz_templates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subjects_assignment_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("subjects_assignments.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    subjects_assignment: Mapped[SubjectsAssignment] = relationship(
        "SubjectsAssignment", back_populates="quiz_template"
    )
    questions: Mapped[list[QuizQuestion]] = relationship(
        "QuizQuestion",
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="QuizQuestion.sort_order",
    )

    __table_args__ = (
        UniqueConstraint("subjects_assignment_id", name="uq_quiz_templates_sa_id"),
        CheckConstraint("version >= 1", name="ck_quiz_templates_version"),
    )


class QuizQuestion(Base, TimestampMixin):
    __tablename__ = "quiz_questions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    template_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("quiz_templates.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[QuizQuestionType] = mapped_column(
        SQLEnum(
            QuizQuestionType,
            name="quiz_question_type",
            native_enum=True,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    template: Mapped[QuizTemplate] = relationship("QuizTemplate", back_populates="questions")

    __table_args__ = (
        Index("ix_quiz_questions_template_id", "template_id"),
        CheckConstraint("points >= 0", name="ck_quiz_questions_points"),
    )


class QuizAttempt(Base, TimestampMixin):
    __tablename__ = "quiz_attempts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    submission_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("submissions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    quiz_template_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("quiz_templates.id", ondelete="RESTRICT"), nullable=False
    )
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    questions_snapshot: Mapped[Any] = mapped_column(JSONB, nullable=False)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    violations: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[QuizAttemptStatus] = mapped_column(
        SQLEnum(
            QuizAttemptStatus,
            name="quiz_attempt_status",
            native_enum=True,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=QuizAttemptStatus.IN_PROGRESS,
    )

    submission: Mapped[Submission] = relationship("Submission", back_populates="quiz_attempts")
    quiz_template: Mapped[QuizTemplate] = relationship("QuizTemplate")
    answers: Mapped[list[QuizAnswer]] = relationship(
        "QuizAnswer", back_populates="attempt", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "score IS NULL OR (score >= 0 AND max_score IS NOT NULL AND score <= max_score)",
            name="ck_quiz_attempts_score",
        ),
        Index("ix_quiz_attempts_submission_id", "submission_id"),
        Index("ix_quiz_attempts_template_id", "quiz_template_id"),
        Index("ix_quiz_attempts_status", "status"),
    )


class QuizAnswer(Base, TimestampMixin):
    __tablename__ = "quiz_answers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    attempt_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("quiz_attempts.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # no FK intentional
    answer: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    is_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    points_earned: Mapped[int | None] = mapped_column(Integer, nullable=True)

    attempt: Mapped[QuizAttempt] = relationship("QuizAttempt", back_populates="answers")

    __table_args__ = (
        Index("ix_quiz_answers_attempt_id", "attempt_id"),
        CheckConstraint(
            "points_earned IS NULL OR points_earned >= 0",
            name="ck_quiz_answers_points_earned",
        ),
    )
