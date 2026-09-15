"""Record of one air-raid pause taken during a quiz attempt.

The attempt itself carries the live state (``paused_at``, ``paused_seconds``); this table is
the audit trail — where the student was, which region's alert justified the pause, and how
long it lasted. A pause stops a graded clock, so it has to be reviewable after the fact.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from submissions_checker.db.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from submissions_checker.db.models.quiz_template import QuizAttempt


class QuizAttemptPause(Base, TimestampMixin):
    """One pause window. ``ended_at`` is NULL while the student is still sheltering."""

    __tablename__ = "quiz_attempt_pauses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    attempt_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("quiz_attempts.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Where the student said they were. Kept so a resolution that looks abusive can be
    # checked against the alert that was actually active at that moment.
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    region_uid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    region_title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    alert_started_at: Mapped[str | None] = mapped_column(String(64), nullable=True)

    attempt: Mapped[QuizAttempt] = relationship("QuizAttempt", back_populates="pauses")

    __table_args__ = (Index("ix_quiz_attempt_pauses_attempt_id", "attempt_id"),)
