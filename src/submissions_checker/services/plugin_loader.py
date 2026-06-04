"""Loads subject plugin configs from the plugins directory into the database on startup."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core.logging import get_logger
from submissions_checker.db.models.subject import Subject
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.db.models.student_assignment import StudentAssignment
from submissions_checker.db.models.subject import SubjectsStudents

logger = get_logger(__name__)


class PluginLoader:
    """Scans plugins_dir for config.yml files and upserts subjects/assignments into DB."""

    async def load_all(self, plugins_dir: Path, db: AsyncSession) -> None:
        if not plugins_dir.exists():
            logger.info("plugins_dir_not_found", path=str(plugins_dir))
            return

        for plugin_dir in sorted(plugins_dir.iterdir()):
            config_file = plugin_dir / "config.yml"
            if not plugin_dir.is_dir() or not config_file.exists():
                continue
            try:
                await self._load_plugin(plugin_dir, config_file, db)
            except Exception as exc:
                logger.error("plugin_load_error", plugin=plugin_dir.name, error=str(exc))

    async def _load_plugin(self, plugin_dir: Path, config_file: Path, db: AsyncSession) -> None:
        raw = config_file.read_bytes()
        content_hash = hashlib.sha256(raw).hexdigest()
        config: dict[str, Any] = yaml.safe_load(raw.decode("utf-8"))

        subject_code: str = config["subjectCode"]

        # Find or create subject
        subject = await self._upsert_subject(db, subject_code, config)

        # Skip if this exact config version already stored
        existing = await db.execute(
            select(SubjectPluginConfig).where(
                SubjectPluginConfig.subject_id == subject.id,
                SubjectPluginConfig.content_hash == content_hash,
            )
        )
        if existing.scalar_one_or_none() is not None:
            logger.info("plugin_config_unchanged", subject_code=subject_code)
        else:
            version = await self._next_version(db, subject.id)
            db.add(SubjectPluginConfig(
                subject_id=subject.id,
                version=version,
                content_hash=content_hash,
                config=config,
                loaded_from=str(config_file),
            ))
            logger.info("plugin_config_loaded", subject_code=subject_code, version=version)

        # Upsert assignments
        assignments_config: dict[str, Any] = config.get("assignments", {})
        for assignment_code, assignment_cfg in assignments_config.items():
            await self._upsert_assignment(db, subject, assignment_code, assignment_cfg)

        await db.flush()

    async def _upsert_subject(
        self, db: AsyncSession, subject_code: str, config: dict[str, Any]
    ) -> Subject:
        result = await db.execute(select(Subject).where(Subject.code == subject_code))
        subject = result.scalar_one_or_none()

        name: str = config.get("name", subject_code)
        description: str | None = config.get("description") or None

        if subject is None:
            subject = Subject(name=name, description=description, code=subject_code)
            db.add(subject)
            await db.flush()
            logger.info("subject_created", subject_code=subject_code, name=name)
        else:
            subject.name = name
            subject.description = description
            logger.info("subject_updated", subject_code=subject_code, name=name)

        return subject

    async def _upsert_assignment(
        self,
        db: AsyncSession,
        subject: Subject,
        assignment_code: str,
        cfg: dict[str, Any],
    ) -> None:
        result = await db.execute(
            select(SubjectsAssignment).where(
                SubjectsAssignment.subject_id == subject.id,
                SubjectsAssignment.code == assignment_code,
            )
        )
        sa = result.scalar_one_or_none()

        deadline = None
        if cfg.get("deadline"):
            try:
                deadline = datetime.fromisoformat(cfg["deadline"]).replace(tzinfo=UTC)
            except (ValueError, TypeError):
                logger.warning("invalid_deadline", assignment=assignment_code, value=cfg["deadline"])

        # Merge all assignment fields into config JSONB (everything except top-level metadata)
        assignment_config: dict[str, Any] = {}
        for key in (
            "review_mode", "late_policy", "max_submissions", "download_links",
            "variants_required", "sandbox", "variants",
        ):
            if key in cfg:
                assignment_config[key] = cfg[key]

        if sa is None:
            sa = SubjectsAssignment(
                subject_id=subject.id,
                code=assignment_code,
                title=cfg.get("title", assignment_code),
                description=cfg.get("description") or None,
                deadline=deadline,
                min_grade=cfg.get("min_grade", 0),
                max_grade=cfg.get("max_grade", 100),
                config=assignment_config,
            )
            db.add(sa)
            await db.flush()

            # Create StudentAssignment records for all currently enrolled students
            enrolled = await db.execute(
                select(SubjectsStudents.student_id).where(SubjectsStudents.subject_id == subject.id)
            )
            for (student_id,) in enrolled:
                db.add(StudentAssignment(student_id=student_id, subjects_assignment_id=sa.id))

            logger.info("assignment_created", subject_code=subject.code, assignment_code=assignment_code)
        else:
            sa.title = cfg.get("title", assignment_code)
            sa.description = cfg.get("description") or None
            sa.deadline = deadline
            sa.min_grade = cfg.get("min_grade", 0)
            sa.max_grade = cfg.get("max_grade", 100)
            sa.config = assignment_config
            logger.info("assignment_updated", subject_code=subject.code, assignment_code=assignment_code)

    async def _next_version(self, db: AsyncSession, subject_id: int) -> int:
        from sqlalchemy import func
        result = await db.execute(
            select(func.coalesce(func.max(SubjectPluginConfig.version), 0)).where(
                SubjectPluginConfig.subject_id == subject_id
            )
        )
        return (result.scalar_one() or 0) + 1
