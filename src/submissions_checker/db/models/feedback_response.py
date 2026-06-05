"""FeedbackResponse model — student's submitted answers."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from submissions_checker.db.models.base import Base


class FeedbackResponse(Base):
    __tablename__ = "feedback_responses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    feedback_token_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("feedback_tokens.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    subject_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False
    )
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    went_well: Mapped[str] = mapped_column(Text, nullable=False)
    went_bad: Mapped[str] = mapped_column(Text, nullable=False)
    to_change: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    token: Mapped[object] = relationship("FeedbackToken")

    __table_args__ = (
        CheckConstraint("rating >= 1 AND rating <= 5", name="ck_feedback_response_rating"),
        Index("ix_feedback_responses_subject_id", "subject_id"),
    )
