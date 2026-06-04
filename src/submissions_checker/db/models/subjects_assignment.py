"""Assignment spec / template belonging to a subject.

``config`` JSONB schema (all keys optional):
  review_mode:    "quiz" | "teacher_review" | "none"   (default: "none")
                  Determines where a submission goes after passing the checker.
                  "quiz"            → status becomes QUIZ_SENT (student takes a quiz)
                  "teacher_review"  → status becomes WAITING_FOR_TEACHER_REVIEW
                  "none"            → status becomes COMPLETED immediately
  download_links: list of {url: str, label: str}       (used by assignment detail template)
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from submissions_checker.db.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from submissions_checker.db.models.quiz_template import QuizTemplate
    from submissions_checker.db.models.student_assignment import StudentAssignment
    from submissions_checker.db.models.subject import Subject


class SubjectsAssignment(Base, TimestampMixin):
    __tablename__ = "subjects_assignments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subject_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    deadline: Mapped[datetime | None] = mapped_column(nullable=True)
    min_grade: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_grade: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    subject: Mapped[Subject] = relationship("Subject", back_populates="assignments")
    students_assignments: Mapped[list[StudentAssignment]] = relationship(
        "StudentAssignment", back_populates="subjects_assignment"
    )
    quiz_template: Mapped[QuizTemplate | None] = relationship(
        "QuizTemplate", back_populates="subjects_assignment", uselist=False
    )

    __table_args__ = (
        CheckConstraint("min_grade >= 0 AND max_grade >= min_grade", name="ck_subjects_assignments_grade_range"),
        Index("ix_subjects_assignments_subject_id", "subject_id"),
    )
