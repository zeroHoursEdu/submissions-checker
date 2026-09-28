"""One anti-cheat event as the server received and judged it — the attempt's timeline."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from submissions_checker.db.models.base import Base, TimestampMixin
from submissions_checker.db.models.enums import QuizEventOutcome

if TYPE_CHECKING:
    from submissions_checker.db.models.quiz_template import QuizAttempt


class QuizAttemptEvent(Base, TimestampMixin):
    """Kept for the life of the attempt, unlike logs, so a disputed fail can be judged later."""

    __tablename__ = "quiz_attempt_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    attempt_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("quiz_attempts.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    count_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rule_threshold: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The config vocabulary (none/warn/reduce_time/flag/fail), not an enum of ours.
    action: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    outcome: Mapped[QuizEventOutcome] = mapped_column(
        SQLEnum(
            QuizEventOutcome,
            name="quiz_event_outcome",
            native_enum=True,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )
    client_ctx: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)

    attempt: Mapped[QuizAttempt] = relationship("QuizAttempt", back_populates="events")

    __table_args__ = (Index("ix_quiz_attempt_events_attempt_id", "attempt_id"),)
