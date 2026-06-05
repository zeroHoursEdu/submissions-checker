"""Student model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, Index, String
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from submissions_checker.db.models.base import Base, TimestampMixin
from submissions_checker.db.models.enums import EntityType

if TYPE_CHECKING:
    from submissions_checker.db.models.group import Group
    from submissions_checker.db.models.student_assignment import StudentAssignment
    from submissions_checker.db.models.user import User


class Student(Base, TimestampMixin):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("groups.id", ondelete="RESTRICT"), nullable=False
    )
    github_username: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[EntityType] = mapped_column(
        SQLEnum(EntityType, name="entity_type", native_enum=True, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=EntityType.REAL,
        server_default="REAL",
    )

    group: Mapped[Group] = relationship("Group", back_populates="students")
    students_assignments: Mapped[list[StudentAssignment]] = relationship(
        "StudentAssignment", back_populates="student"
    )
    user: Mapped[User | None] = relationship("User", back_populates="student", uselist=False)

    __table_args__ = (Index("ix_students_group_id", "group_id"),)
