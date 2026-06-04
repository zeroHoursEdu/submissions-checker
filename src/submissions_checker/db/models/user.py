"""User model for authentication."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Index, String
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from submissions_checker.db.models.base import Base, TimestampMixin
from submissions_checker.db.models.enums import UserRole

if TYPE_CHECKING:
    from submissions_checker.db.models.student import Student
    from submissions_checker.db.models.user_login import UserLogin


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        SQLEnum(UserRole, name="user_role", native_enum=True, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    student_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("students.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    student: Mapped[Student | None] = relationship("Student", back_populates="user")
    logins: Mapped[list[UserLogin]] = relationship("UserLogin", back_populates="user")

    __table_args__ = (
        Index("ix_users_username", "username"),
        Index("ix_users_student_id", "student_id"),
        CheckConstraint(
            "(role = 'STUDENT' AND student_id IS NOT NULL) OR role IN ('TEACHER', 'ADMIN')",
            name="ck_users_student_role_has_student_id",
        ),
    )
