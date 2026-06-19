"""Webcam proctoring snapshot captured on a flagged quiz anti-cheat event."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from submissions_checker.db.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from submissions_checker.db.models.quiz_template import QuizAttempt


class QuizAttemptSnapshot(Base, TimestampMixin):
    """Evidence frame stored to object storage when a proctoring event is flagged."""

    __tablename__ = "quiz_attempt_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    attempt_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("quiz_attempts.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    s3_key: Mapped[str] = mapped_column(String(512), nullable=False)
    s3_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    attempt: Mapped[QuizAttempt] = relationship("QuizAttempt", back_populates="snapshots")

    __table_args__ = (Index("ix_quiz_attempt_snapshots_attempt_id", "attempt_id"),)
