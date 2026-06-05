"""Subject (course) model and enrollment association."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from submissions_checker.db.models.base import Base, TimestampMixin
from submissions_checker.db.models.enums import SubjectStatus

if TYPE_CHECKING:
    from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
    from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
    from submissions_checker.db.models.user import User


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
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    github_repo: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Plugin identifier — used exclusively to match/upsert Subject from config.yml
    code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    grid_picture_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    main_picture_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # Ownership: the teacher who created this subject via ZIP upload (NULL for startup-loaded subjects)
    owner_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[SubjectStatus] = mapped_column(
        SQLEnum(SubjectStatus, name="subject_status", native_enum=True, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=SubjectStatus.ACTIVE,
        server_default="ACTIVE",
    )

    assignments: Mapped[list[SubjectsAssignment]] = relationship(
        "SubjectsAssignment", back_populates="subject"
    )
    plugin_configs: Mapped[list[SubjectPluginConfig]] = relationship(
        "SubjectPluginConfig", back_populates="subject", order_by="SubjectPluginConfig.version"
    )
    owner: Mapped[User | None] = relationship("User", foreign_keys=[owner_id])

    __table_args__ = (
        # Only one ACTIVE subject per code; unlimited DELETED rows with same code are allowed
        Index(
            "uix_subjects_active_code",
            "code",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index("ix_subjects_status", "status"),
        Index("ix_subjects_owner_id", "owner_id"),
    )
