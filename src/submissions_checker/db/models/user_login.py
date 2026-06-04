"""UserLogin model — tracks each successful login for activity reporting."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from submissions_checker.db.models.base import Base

if TYPE_CHECKING:
    from submissions_checker.db.models.user import User


class UserLogin(Base):
    __tablename__ = "user_logins"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    logged_in_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[User] = relationship("User", back_populates="logins")

    __table_args__ = (
        Index("ix_user_logins_user_id", "user_id"),
        Index("ix_user_logins_logged_in_at", "logged_in_at"),
    )
