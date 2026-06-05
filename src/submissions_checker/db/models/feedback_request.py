"""FeedbackRequest model — one per subject per semester."""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from submissions_checker.db.models.base import Base, TimestampMixin


class FeedbackRequest(Base, TimestampMixin):
    __tablename__ = "feedback_requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subject_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False
    )
    semester_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("semesters.id"), nullable=False
    )
    created_by_teacher_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False
    )

    semester: Mapped[object] = relationship("Semester")
    tokens: Mapped[list[object]] = relationship("FeedbackToken", back_populates="feedback_request")

    __table_args__ = (
        UniqueConstraint("subject_id", "semester_id", name="uq_feedback_request_subject_semester"),
        Index("ix_feedback_requests_subject_id", "subject_id"),
    )
