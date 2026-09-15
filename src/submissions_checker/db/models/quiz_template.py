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
    from submissions_checker.db.models.quiz_attempt_pause import QuizAttemptPause
    from submissions_checker.db.models.quiz_attempt_snapshot import QuizAttemptSnapshot
    from submissions_checker.db.models.quiz_dispute import QuizQuestionDispute
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
    # Per-question timing cursor. Only meaningful when config_snapshot["per_question_timing"]
    # is set; otherwise the whole attempt is answered on one page and these stay at 0/NULL.
    current_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    question_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Air-raid pause. Non-NULL means the attempt is paused RIGHT NOW and every clock is
    # frozen; `paused_seconds` accumulates the pauses that have already closed. Kept in
    # columns rather than in the `violations` JSONB because a pause is not a violation and
    # the timing helpers must be able to read it without parsing anti-cheat bookkeeping.
    # Invariant: a paused attempt is always IN_PROGRESS — finalizing clears `paused_at`.
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paused_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
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
    snapshots: Mapped[list[QuizAttemptSnapshot]] = relationship(
        "QuizAttemptSnapshot", back_populates="attempt", cascade="all, delete-orphan"
    )
    disputes: Mapped[list[QuizQuestionDispute]] = relationship(
        "QuizQuestionDispute", back_populates="attempt", cascade="all, delete-orphan"
    )
    pauses: Mapped[list[QuizAttemptPause]] = relationship(
        "QuizAttemptPause", back_populates="attempt", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "score IS NULL OR (score >= 0 AND max_score IS NOT NULL AND score <= max_score)",
            name="ck_quiz_attempts_score",
        ),
        CheckConstraint("paused_seconds >= 0", name="ck_quiz_attempts_paused_seconds"),
        Index("ix_quiz_attempts_submission_id", "submission_id"),
        Index("ix_quiz_attempts_status", "status"),
    )


class QuizAnswer(Base, TimestampMixin):
    __tablename__ = "quiz_answers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    attempt_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("quiz_attempts.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )  # no FK intentional — references snapshot index
    answer: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    is_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    points_earned: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # True when the question's own time window closed before a valid answer arrived. Scores
    # zero either way; the flag is what lets the result page (and dashboards) tell a
    # wrong answer apart from one lost to the clock.
    timed_out: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    attempt: Mapped[QuizAttempt] = relationship("QuizAttempt", back_populates="answers")

    __table_args__ = (
        Index("ix_quiz_answers_attempt_id", "attempt_id"),
        CheckConstraint(
            "points_earned IS NULL OR points_earned >= 0",
            name="ck_quiz_answers_points_earned",
        ),
    )
