"""A teacher's Google account connection (encrypted OAuth refresh token)."""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from submissions_checker.db.models.base import Base, TimestampMixin
from submissions_checker.db.models.enums import GoogleConnectionStatus


class GoogleConnection(Base, TimestampMixin):
    __tablename__ = "google_connections"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    google_email: Mapped[str] = mapped_column(String(255), nullable=False)
    # Fernet token; the plaintext refresh token is never stored.
    refresh_token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=GoogleConnectionStatus.ACTIVE.value
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
