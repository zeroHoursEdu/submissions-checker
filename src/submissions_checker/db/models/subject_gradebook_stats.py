"""Cached, scheduled-job-computed subject-wide gradebook stats.

Written by workers.scheduled.subject_stats_refresh on a ~5 minute interval
(subject_stats_refresh_interval setting) — read live by the teacher_subject
route instead of being computed per request. See gradebook.compute_cached_stats
for the pure calculation this table's values come from.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from submissions_checker.db.models.base import Base


class SubjectGradebookStats(Base):
    __tablename__ = "subject_gradebook_stats"

    subject_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("subjects.id", ondelete="CASCADE"), primary_key=True
    )
    pending_review_count: Mapped[int] = mapped_column(Integer, nullable=False)
    average_mark_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    pass_pct: Mapped[float] = mapped_column(Float, nullable=False)
    cheating_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
