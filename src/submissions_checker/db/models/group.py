"""Student group (cohort) model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, String, Text
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from submissions_checker.db.models.base import Base, TimestampMixin
from submissions_checker.db.models.enums import EntityType

if TYPE_CHECKING:
    from submissions_checker.db.models.student import Student


class Group(Base, TimestampMixin):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[EntityType] = mapped_column(
        SQLEnum(EntityType, name="entity_type", native_enum=True, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=EntityType.REAL,
        server_default="REAL",
    )

    students: Mapped[list[Student]] = relationship("Student", back_populates="group")
