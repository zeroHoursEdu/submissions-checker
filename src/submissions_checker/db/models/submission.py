"""Concrete submission attempt — provider-agnostic, stores source metadata as JSONB."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, ForeignKey, Index, String
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from submissions_checker.db.models.base import Base, TimestampMixin
from submissions_checker.db.models.enums import SubmissionSourceType, SubmissionStatus

if TYPE_CHECKING:
    from submissions_checker.db.models.quiz_template import QuizAttempt  # noqa: F401
    from submissions_checker.db.models.student_assignment import StudentAssignment


class Submission(Base, TimestampMixin):
    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    students_assignment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("students_assignments.id", ondelete="RESTRICT"), nullable=False
    )
    source_type: Mapped[SubmissionSourceType] = mapped_column(
        SQLEnum(
            SubmissionSourceType,
            name="submission_source_type",
            native_enum=True,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )
    source_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    repository_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[SubmissionStatus] = mapped_column(
        SQLEnum(
            SubmissionStatus,
            name="submission_status",
            native_enum=True,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=SubmissionStatus.PENDING,
    )
    test_results: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ai_review: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    students_assignment: Mapped[StudentAssignment] = relationship(
        "StudentAssignment", back_populates="submissions"
    )
    quiz_attempts: Mapped[list[QuizAttempt]] = relationship(
        "QuizAttempt", back_populates="submission", uselist=True, order_by="QuizAttempt.started_at"
    )

    __table_args__ = (
        Index("ix_submissions_students_assignment_id", "students_assignment_id"),
        Index("ix_submissions_status", "status"),
    )
