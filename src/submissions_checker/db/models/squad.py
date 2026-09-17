"""Squads: a few students who hand in one submission per assignment and share its grade.

One squad per (subject, student). Members are immutable once ``locked_at`` is set —
the squad reached ``subjects.squad_max_size`` or made its first submission. Fixes to a
locked squad are a DB job on purpose (see docs/features/squads.md).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from submissions_checker.db.models.base import Base, TimestampMixin
from submissions_checker.db.models.enums import SquadInviteStatus

if TYPE_CHECKING:
    from submissions_checker.db.models.student import Student


class Squad(Base, TimestampMixin):
    __tablename__ = "squads"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subject_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_by_student_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("students.id", ondelete="SET NULL"), nullable=True
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    members: Mapped[list[SquadMember]] = relationship(
        "SquadMember", back_populates="squad", cascade="all, delete-orphan"
    )
    invites: Mapped[list[SquadInvite]] = relationship(
        "SquadInvite", back_populates="squad", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_squads_subject_id", "subject_id"),)


class SquadMember(Base):
    __tablename__ = "squad_members"

    squad_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("squads.id", ondelete="CASCADE"), primary_key=True
    )
    student_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("students.id", ondelete="CASCADE"), primary_key=True
    )
    # Denormalised from squads.subject_id so the one-squad-per-subject rule is a plain
    # unique constraint instead of a trigger.
    subject_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    squad: Mapped[Squad] = relationship("Squad", back_populates="members")
    student: Mapped[Student] = relationship("Student")

    __table_args__ = (
        UniqueConstraint("subject_id", "student_id", name="uq_squad_members_subject_student"),
        Index("ix_squad_members_student_id", "student_id"),
    )


class SquadInvite(Base, TimestampMixin):
    __tablename__ = "squad_invites"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    squad_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("squads.id", ondelete="CASCADE"), nullable=False
    )
    invited_student_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("students.id", ondelete="CASCADE"), nullable=False
    )
    invited_by_student_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("students.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[SquadInviteStatus] = mapped_column(
        SQLEnum(
            SquadInviteStatus,
            name="squad_invite_status",
            native_enum=True,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=SquadInviteStatus.PENDING,
        server_default="PENDING",
    )

    squad: Mapped[Squad] = relationship("Squad", back_populates="invites")
    invited_student: Mapped[Student] = relationship("Student", foreign_keys=[invited_student_id])

    __table_args__ = (
        Index("ix_squad_invites_squad_id", "squad_id"),
        Index("ix_squad_invites_invited_student_id", "invited_student_id"),
        Index(
            "uq_squad_invites_pending",
            "squad_id",
            "invited_student_id",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
    )
