"""Password reset token model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from submissions_checker.db.models.base import Base, TimestampMixin


class PasswordResetToken(Base, TimestampMixin):
    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_password_reset_tokens_token", "token"),
        Index("ix_password_reset_tokens_user_id", "user_id"),
    )

    @classmethod
    def create(cls, user_id: int, token: str, ttl_hours: int = 2) -> "PasswordResetToken":
        return cls(
            user_id=user_id,
            token=token,
            expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours),
        )

    def is_valid(self) -> bool:
        return not self.used and datetime.now(UTC) < self.expires_at.replace(tzinfo=UTC)
