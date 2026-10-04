"""Google Classroom ingest: roster links, fetched works, LLM grading drafts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from submissions_checker.db.models.base import Base, TimestampMixin


class ClassroomStudentLink(Base, TimestampMixin):
    """One Classroom roster entry of a subject and the platform student it maps to."""

    __tablename__ = "classroom_student_links"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subject_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False
    )
    classroom_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    classroom_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    classroom_name: Mapped[str] = mapped_column(String(255), nullable=False)
    student_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("students.id", ondelete="SET NULL"), nullable=True
    )
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    candidates: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    __table_args__ = (
        UniqueConstraint("subject_id", "classroom_user_id", name="uq_classroom_links_subject_user"),
    )


class ClassroomWork(Base, TimestampMixin):
    """One fetched version (by content hash) of a student's Classroom submission."""

    __tablename__ = "classroom_works"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subjects_assignment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("subjects_assignments.id", ondelete="CASCADE"), nullable=False
    )
    link_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("classroom_student_links.id", ondelete="CASCADE"), nullable=False
    )
    classroom_submission_id: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    late: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    link: Mapped[ClassroomStudentLink] = relationship("ClassroomStudentLink")

    __table_args__ = (
        UniqueConstraint(
            "classroom_submission_id", "content_hash", name="uq_classroom_works_sub_hash"
        ),
        Index("ix_classroom_works_assignment_link", "subjects_assignment_id", "link_id"),
    )


class LLMGrading(Base, TimestampMixin):
    """The LLM's draft grade for one work, awaiting teacher approval."""

    __tablename__ = "llm_gradings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    classroom_work_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("classroom_works.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    draft: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    graded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    work: Mapped[ClassroomWork] = relationship("ClassroomWork")

    __table_args__ = (Index("ix_llm_gradings_status", "status"),)
