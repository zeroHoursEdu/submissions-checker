"""Subject (course) model and enrollment association."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from submissions_checker.db.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
    from submissions_checker.db.models.subjects_assignment import SubjectsAssignment


class SubjectsStudents(Base):
    """Enrollment: which students are enrolled in which subjects."""

    __tablename__ = "subjects_students"

    subject_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("subjects.id", ondelete="CASCADE"), primary_key=True
    )
    student_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("students.id", ondelete="CASCADE"), primary_key=True
    )
    enrolled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    __table_args__ = (Index("ix_subjects_students_student_id", "student_id"),)


class Subject(Base, TimestampMixin):
    __tablename__ = "subjects"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    github_repo: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Plugin identifier — used exclusively to match/upsert Subject from config.yml; not displayed
    code: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)

    assignments: Mapped[list[SubjectsAssignment]] = relationship(
        "SubjectsAssignment", back_populates="subject"
    )
    plugin_configs: Mapped[list[SubjectPluginConfig]] = relationship(
        "SubjectPluginConfig", back_populates="subject", order_by="SubjectPluginConfig.version"
    )
