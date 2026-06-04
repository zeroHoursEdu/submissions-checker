"""Assignment spec / template belonging to a subject.

``config`` JSONB is populated from config.yml by PluginLoader on startup; UI fields are
deprecated and will be overwritten on restart when a plugin config is present.

``config`` JSONB schema (all keys optional):
  review_mode:       "tests_only" | "tests_then_ai" | "tests_then_teacher" |
                     "tests_then_ai_then_teacher" | "tests_then_quiz"  (default: "tests_only")
  late_policy:       "block" | "allow"                                 (default: "block")
  max_submissions:   int                                               (optional)
  download_links:    list of {url: str, label: str}                   (optional)
  variants_required: bool                                              (default: false)
  sandbox:           dict — image, tool, commands, resource limits, visibility flags
  variants:          dict[str, dict] — per-variant command overrides
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from submissions_checker.db.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from submissions_checker.db.models.student_assignment import StudentAssignment
    from submissions_checker.db.models.subject import Subject


class SubjectsAssignment(Base, TimestampMixin):
    __tablename__ = "subjects_assignments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subject_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False
    )
    # Plugin key inside config.yml assignments block — used for upsert matching, similar to subjects.code
    code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    deadline: Mapped[datetime | None] = mapped_column(nullable=True)
    min_grade: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_grade: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Uploaded content files (PDFs, docs) for this assignment — [{url, display_name, filename}]
    content_files: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)

    subject: Mapped[Subject] = relationship("Subject", back_populates="assignments")
    students_assignments: Mapped[list[StudentAssignment]] = relationship(
        "StudentAssignment", back_populates="subjects_assignment"
    )

    __table_args__ = (
        CheckConstraint("min_grade >= 0 AND max_grade >= min_grade", name="ck_subjects_assignments_grade_range"),
        UniqueConstraint("subject_id", "code", name="uq_subjects_assignments_subject_code"),
        Index("ix_subjects_assignments_subject_id", "subject_id"),
    )
