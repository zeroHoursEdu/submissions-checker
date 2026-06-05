"""FeedbackToken model — single-use tokenised link per student per request."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from submissions_checker.db.models.base import Base


class FeedbackToken(Base):
    __tablename__ = "feedback_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    feedback_request_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("feedback_requests.id", ondelete="CASCADE"), nullable=False
    )
    student_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("students.id", ondelete="CASCADE"), nullable=False
    )
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    feedback_request: Mapped[object] = relationship("FeedbackRequest", back_populates="tokens")
    student: Mapped[object] = relationship("Student")

    __table_args__ = (
        Index("ix_feedback_tokens_feedback_request_id", "feedback_request_id"),
    )
