"""Pending teacher-review notifications, coalesced into digest emails by the flush job."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from submissions_checker.db.models.base import Base, TimestampMixin


class TeacherNotificationQueue(Base, TimestampMixin):
    """One pending entry per (teacher, submission) awaiting a digest email.

    Rows are enqueued transactionally when a submission enters AWAITING_TEACHER_REVIEW.
    The teacher_digest_processor groups unsent rows per teacher and sends a single email,
    stamping ``sent_at`` so entries are never re-sent.
    """

    __tablename__ = "teacher_notification_queue"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    teacher_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    submission_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("teacher_id", "submission_id", name="uq_teacher_notification_queue"),
        Index("ix_teacher_notification_queue_pending", "teacher_id", "sent_at", "created_at"),
    )
