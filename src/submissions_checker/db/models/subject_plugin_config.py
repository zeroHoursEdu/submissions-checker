"""Versioned plugin configuration for a subject, loaded from config.yml on startup."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, LargeBinary, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from submissions_checker.db.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from submissions_checker.db.models.subject import Subject


class SubjectPluginConfig(Base, TimestampMixin):
    """One record per distinct config.yml version loaded for a subject.

    version is auto-incremented per subject (1, 2, 3…).
    content_hash prevents duplicate inserts of unchanged files.
    Fetch latest config with ORDER BY version DESC LIMIT 1.
    """

    __tablename__ = "subject_plugin_configs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subject_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    loaded_from: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Raw ZIP bytes from UI upload; NULL for rows inserted by the startup PluginLoader
    zip_data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    subject: Mapped[Subject] = relationship("Subject", back_populates="plugin_configs")

    __table_args__ = (
        UniqueConstraint("subject_id", "version", name="uq_subject_plugin_configs_subject_version"),
        UniqueConstraint("subject_id", "content_hash", name="uq_subject_plugin_configs_subject_hash"),
        Index("ix_subject_plugin_configs_subject_version", "subject_id", "version"),
    )
