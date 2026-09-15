"""Student reports that a quiz question is broken, and the credit a teacher grants.

Two tables, because a dispute and its consequence have different lifetimes. The dispute is
one student's complaint about one attempt. The *override* is the teacher's ruling about a
question in a plugin config version — it outlives the attempt that surfaced it and applies
to every student who drew that question, including attempts still in progress and attempts
started after the ruling.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from submissions_checker.db.models.base import Base, TimestampMixin
from submissions_checker.db.models.enums import QuizDisputeStatus

if TYPE_CHECKING:
    from submissions_checker.db.models.quiz_template import QuizAttempt


class QuizQuestionDispute(Base, TimestampMixin):
    """One student's report that one question of their attempt is invalid.

    Deliberately carries no unique constraint on ``(attempt_id, question_id)``: a student
    may file more than once, and an accepted ruling resolves every open sibling report on
    the same question at once, so duplicates cost the teacher nothing.
    """

    __tablename__ = "quiz_question_disputes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    attempt_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("quiz_attempts.id", ondelete="CASCADE"), nullable=False
    )
    # The 0-based index into the plugin config's `questions` list — no FK, exactly the
    # convention QuizAnswer.question_id follows, because questions are not DB rows.
    question_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    student_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("students.id", ondelete="CASCADE"), nullable=False
    )
    # Copied off the attempt when the report is filed, so the ruling's blast radius is
    # pinned even if the attempt's config pointer is later nulled out.
    plugin_config_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("subject_plugin_configs.id", ondelete="SET NULL"), nullable=True
    )
    plugin_config_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    student_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[QuizDisputeStatus] = mapped_column(
        SQLEnum(
            QuizDisputeStatus,
            name="quiz_dispute_status",
            native_enum=True,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=QuizDisputeStatus.OPEN,
    )
    # Mandatory once resolved: the whole point of the panel is that the teacher says why.
    teacher_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    attempt: Mapped[QuizAttempt] = relationship("QuizAttempt", back_populates="disputes")

    __table_args__ = (
        Index("ix_quiz_question_disputes_attempt_id", "attempt_id"),
        Index("ix_quiz_question_disputes_status", "status", "created_at"),
        Index(
            "ix_quiz_question_disputes_scope",
            "plugin_config_id",
            "plugin_config_version",
            "question_id",
        ),
    )


class QuizQuestionOverride(Base, TimestampMixin):
    """A teacher's ruling that one question of one config version is credited to everyone.

    Scoring consults this table rather than the answer rows alone, which is what lets an
    accepted ruling reach attempts that are still in progress — and attempts not yet
    started — without pre-writing answer rows. Pre-writing would double-count: answering a
    question always INSERTs a fresh QuizAnswer (``answer_question``) and
    ``quiz_answers`` has no unique constraint on ``(attempt_id, question_id)``.

    Scoped to an exact config version: re-uploading a subject's config bumps the version,
    so a question that was repaired stops being credited while the stale index does not
    silently credit whatever question moved into its place.
    """

    __tablename__ = "quiz_question_overrides"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plugin_config_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("subject_plugin_configs.id", ondelete="CASCADE"),
        nullable=False,
    )
    plugin_config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    question_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Always True today. The column exists so a future "drop the question from max_score"
    # ruling is a value, not a migration.
    credit_all: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    dispute_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("quiz_question_disputes.id", ondelete="SET NULL"), nullable=True
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "plugin_config_id",
            "plugin_config_version",
            "question_id",
            name="uq_quiz_question_override",
        ),
    )
