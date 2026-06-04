"""Loads subject plugin configs from the plugins directory into the database on startup."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core.logging import get_logger
from submissions_checker.db.models.subject import Subject
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.db.models.student_assignment import StudentAssignment
from submissions_checker.db.models.subject import SubjectsStudents

if TYPE_CHECKING:
    from submissions_checker.services.storage import StorageService

logger = get_logger(__name__)


class PluginLoader:
    """Scans plugins_dir for config.yml files and upserts subjects/assignments into DB."""

    async def load_all(
        self, plugins_dir: Path, db: AsyncSession, storage: StorageService | None = None
    ) -> None:
        if not plugins_dir.exists():
            logger.info("plugins_dir_not_found", path=str(plugins_dir))
            return

        for plugin_dir in sorted(plugins_dir.iterdir()):
            config_file = plugin_dir / "config.yml"
            if not plugin_dir.is_dir() or not config_file.exists():
                continue
            try:
                await self._load_plugin(plugin_dir, config_file, db, storage)
            except Exception as exc:
                logger.error("plugin_load_error", plugin=plugin_dir.name, error=str(exc))

    async def _load_plugin(
        self,
        plugin_dir: Path,
        config_file: Path,
        db: AsyncSession,
        storage: StorageService | None,
    ) -> None:
        raw = config_file.read_bytes()
        content_hash = hashlib.sha256(raw).hexdigest()
        config: dict[str, Any] = yaml.safe_load(raw.decode("utf-8"))

        subject_code: str = config["subjectCode"]

        # Find or create subject
        subject = await self._upsert_subject(db, subject_code, config)

        # Upload subject images if storage is available
        if storage is not None:
            await self._upload_subject_images(plugin_dir, subject_code, config, subject, storage)

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
            sa = await self._upsert_assignment(db, subject, assignment_code, assignment_cfg)
            if storage is not None and sa is not None:
                await self._upload_assignment_files(
                    plugin_dir, subject_code, assignment_code, assignment_cfg, sa, storage, db
                )

        await db.flush()

    async def _upload_subject_images(
        self,
        plugin_dir: Path,
        subject_code: str,
        config: dict[str, Any],
        subject: Subject,
        storage: StorageService,
    ) -> None:
        for field, attr in (("gridPicture", "grid_picture_url"), ("mainPicture", "main_picture_url")):
            filename: str | None = config.get(field)
            if not filename:
                continue
            local_path = plugin_dir / filename
            if not local_path.exists():
                logger.warning("subject_image_missing", subject_code=subject_code, field=field, path=str(local_path))
                continue
            key = f"subjects/{subject_code}/images/{filename}"
            try:
                url = await storage.upload_file(local_path, key)
                setattr(subject, attr, url)
            except Exception as exc:
                logger.error("subject_image_upload_failed", subject_code=subject_code, field=field, error=str(exc))

    async def _upload_assignment_files(
        self,
        plugin_dir: Path,
        subject_code: str,
        assignment_code: str,
        cfg: dict[str, Any],
        sa: SubjectsAssignment,
        storage: StorageService,
        db: AsyncSession,
    ) -> None:
        raw_files: list[dict[str, Any]] = cfg.get("contentFiles", [])
        if not raw_files:
            return

        uploaded: list[dict[str, str]] = []
        for entry in raw_files:
            filename: str | None = entry.get("filename")
            display_name: str = entry.get("displayName", filename or "")
            if not filename:
                continue
            local_path = plugin_dir / "assignments" / assignment_code / filename
            if not local_path.exists():
                logger.warning(
                    "content_file_missing",
                    subject_code=subject_code,
                    assignment_code=assignment_code,
                    filename=filename,
                    path=str(local_path),
                )
                continue
            key = f"subjects/{subject_code}/assignments/{assignment_code}/{filename}"
            try:
                url = await storage.upload_file(local_path, key)
                uploaded.append({"url": url, "display_name": display_name, "filename": filename})
            except Exception as exc:
                logger.error(
                    "content_file_upload_failed",
                    subject_code=subject_code,
                    assignment_code=assignment_code,
                    filename=filename,
                    error=str(exc),
                )

        if uploaded:
            sa.content_files = uploaded

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
    ) -> SubjectsAssignment | None:
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

        return sa

    async def _next_version(self, db: AsyncSession, subject_id: int) -> int:
        from sqlalchemy import func
        result = await db.execute(
            select(func.coalesce(func.max(SubjectPluginConfig.version), 0)).where(
                SubjectPluginConfig.subject_id == subject_id
            )
        )
        return (result.scalar_one() or 0) + 1
