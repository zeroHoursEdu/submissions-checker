"""Quiz attempt and answer models. Questions live in config.yml (plugin config), not the DB."""

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
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from submissions_checker.db.models.base import Base, TimestampMixin
from submissions_checker.db.models.enums import QuizAttemptStatus

if TYPE_CHECKING:
    from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
    from submissions_checker.db.models.submission import Submission


class QuizAttempt(Base, TimestampMixin):
    __tablename__ = "quiz_attempts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    submission_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("submissions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    plugin_config_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("subject_plugin_configs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    plugin_config_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    plugin_config: Mapped[SubjectPluginConfig | None] = relationship("SubjectPluginConfig")
    answers: Mapped[list[QuizAnswer]] = relationship(
        "QuizAnswer", back_populates="attempt", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "score IS NULL OR (score >= 0 AND max_score IS NOT NULL AND score <= max_score)",
            name="ck_quiz_attempts_score",
        ),
        Index("ix_quiz_attempts_submission_id", "submission_id"),
        Index("ix_quiz_attempts_status", "status"),
    )


class QuizAnswer(Base, TimestampMixin):
    __tablename__ = "quiz_answers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    attempt_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("quiz_attempts.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # no FK intentional — references snapshot index
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
