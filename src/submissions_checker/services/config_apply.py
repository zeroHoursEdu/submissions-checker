"""ZIP-driven subject config apply service.

Accepts a ZIP file uploaded by a teacher, extracts it, computes a field-level
diff against the current DB state, logs the plan, then executes it in the
required order: S3 uploads → DB transaction → S3 cleanup.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core.logging import get_logger
from submissions_checker.db.models.enums import SubjectStatus
from submissions_checker.db.models.student_assignment import StudentAssignment
from submissions_checker.db.models.subject import Subject, SubjectsStudents
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.utils.safe_zip import UnsafeArchiveError, safe_extract

if TYPE_CHECKING:
    from submissions_checker.services.storage import StorageService

logger = get_logger(__name__)

_MAX_ZIP_BYTES = 50 * 1024 * 1024  # 50 MB


# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------

@dataclass
class ConfigApplyPlan:
    subject_action: Literal["create", "update", "none"]
    subject_fields_changed: list[str] = field(default_factory=list)
    new_s3_files: list[tuple[Path, str]] = field(default_factory=list)   # (local_path, s3_key)
    removed_s3_keys: list[str] = field(default_factory=list)
    assignments_to_create: list[str] = field(default_factory=list)       # assignment codes
    assignments_to_update: list[tuple[str, list[str]]] = field(default_factory=list)  # (code, changed_fields)
    assignments_to_delete: list[str] = field(default_factory=list)


@dataclass
class ApplyResult:
    changed: bool
    subject_action: str  # "created" | "updated" | "unchanged"
    subject_name: str


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class ConfigApplyService:
    def __init__(self, storage: StorageService | None) -> None:
        self._storage = storage

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def apply(
        self,
        zip_bytes: bytes,
        owner_id: int,
        db: AsyncSession,
    ) -> ApplyResult:
        if len(zip_bytes) > _MAX_ZIP_BYTES:
            raise ValueError(f"ZIP file exceeds the 50 MB limit ({len(zip_bytes) // (1024 * 1024)} MB received)")

        if not zipfile.is_zipfile(io.BytesIO(zip_bytes)):
            raise ValueError("Uploaded file is not a valid ZIP archive")

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp_dir = Path(tmp_str)
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                try:
                    safe_extract(zf, tmp_dir)
                except UnsafeArchiveError as exc:
                    raise ValueError(f"Uploaded ZIP archive is unsafe: {exc}") from exc

            config_path = tmp_dir / "config.yml"
            if not config_path.exists():
                raise ValueError("ZIP archive must contain config.yml at its root")

            new_cfg: dict[str, Any] = yaml.safe_load(config_path.read_text("utf-8"))

        subject_code: str = new_cfg.get("subjectCode", "")
        if not subject_code:
            raise ValueError("config.yml must contain a non-empty 'subjectCode' field")

        sha256 = hashlib.sha256(zip_bytes).hexdigest()

        # Ownership + existing-subject lookup
        subject, prev_cfg = await self._check_ownership(db, subject_code, owner_id)

        # Deduplication: if the ZIP hash matches the latest stored version, skip
        if subject is not None:
            dup = await self._check_duplicate(db, subject.id, sha256)
            if dup is not None:
                logger.info("config_apply_unchanged", subject_code=subject_code, sha256=sha256)
                return dup

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp_dir = Path(tmp_str)
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                try:
                    safe_extract(zf, tmp_dir)
                except UnsafeArchiveError as exc:
                    raise ValueError(f"Uploaded ZIP archive is unsafe: {exc}") from exc

            plan = self._compute_plan(new_cfg, prev_cfg, subject, tmp_dir)

            self._log_plan(subject_code, plan)

            return await self._execute_plan(plan, new_cfg, zip_bytes, sha256, subject, owner_id, subject_code, db, tmp_dir)

    # ------------------------------------------------------------------
    # Helpers: pre-flight checks
    # ------------------------------------------------------------------

    async def _check_ownership(
        self,
        db: AsyncSession,
        subject_code: str,
        owner_id: int,
    ) -> tuple[Subject | None, dict[str, Any]]:
        """Return (subject_or_none, prev_config_jsonb).

        Raises PermissionError if subject exists and current user is not the owner.
        """
        result = await db.execute(
            select(Subject).where(
                Subject.code == subject_code,
                Subject.status == SubjectStatus.ACTIVE,
            )
        )
        subject = result.scalar_one_or_none()

        if subject is None:
            return None, {}

        if subject.owner_id is not None and subject.owner_id != owner_id:
            raise PermissionError(
                f"Subject '{subject_code}' is owned by another teacher. Only its owner can apply config updates."
            )

        # Load the latest stored config JSONB to diff against
        latest_cfg_result = await db.execute(
            select(SubjectPluginConfig.config)
            .where(SubjectPluginConfig.subject_id == subject.id)
            .order_by(SubjectPluginConfig.version.desc())
            .limit(1)
        )
        prev_cfg: dict[str, Any] = latest_cfg_result.scalar_one_or_none() or {}

        return subject, prev_cfg

    async def _check_duplicate(
        self,
        db: AsyncSession,
        subject_id: int,
        sha256: str,
    ) -> ApplyResult | None:
        """Return ApplyResult(changed=False) if this exact ZIP was already applied."""
        result = await db.execute(
            select(SubjectPluginConfig.id).where(
                SubjectPluginConfig.subject_id == subject_id,
                SubjectPluginConfig.content_hash == sha256,
            )
        )
        if result.scalar_one_or_none() is not None:
            return ApplyResult(changed=False, subject_action="unchanged", subject_name="")
        return None

    # ------------------------------------------------------------------
    # Diff computation (field-level)
    # ------------------------------------------------------------------

    def _compute_plan(
        self,
        new_cfg: dict[str, Any],
        prev_cfg: dict[str, Any],
        subject: Subject | None,
        tmp_dir: Path,
    ) -> ConfigApplyPlan:
        plan = ConfigApplyPlan(subject_action="create" if subject is None else "update")

        # --- Subject metadata diff ---
        meta_map = {
            "name": ("name", new_cfg.get("name", new_cfg.get("subjectCode", ""))),
            "description": ("description", new_cfg.get("description") or None),
            "github_repo": ("github_repo", new_cfg.get("githubRepo") or None),
        }
        for field_name, (attr, new_val) in meta_map.items():
            current_val = getattr(subject, attr, None) if subject else None
            if new_val != current_val:
                plan.subject_fields_changed.append(field_name)

        prev_assignments_cfg: dict[str, Any] = prev_cfg.get("assignments", {})

        # --- S3 image diff ---
        for cfg_key, s3_suffix in (("gridPicture", "grid_picture_url"), ("mainPicture", "main_picture_url")):
            new_filename: str | None = new_cfg.get(cfg_key)
            prev_filename: str | None = prev_cfg.get(cfg_key)
            current_url: str | None = getattr(subject, s3_suffix, None) if subject else None

            if new_filename and new_filename != prev_filename:
                local_path = tmp_dir / new_filename
                if local_path.exists():
                    s3_key = f"subjects/{new_cfg['subjectCode']}/images/{new_filename}"
                    plan.new_s3_files.append((local_path, s3_key))
                    plan.subject_fields_changed.append(s3_suffix)

            if prev_filename and prev_filename != new_filename and current_url:
                old_key = f"subjects/{new_cfg['subjectCode']}/images/{prev_filename}"
                plan.removed_s3_keys.append(old_key)

        # --- Assignment diff ---
        new_assignments_cfg: dict[str, Any] = new_cfg.get("assignments", {})

        for code, new_a_cfg in new_assignments_cfg.items():
            if code not in prev_assignments_cfg:
                plan.assignments_to_create.append(code)
                # All content files for a new assignment are new S3 uploads
                self._collect_new_content_files(
                    tmp_dir, new_cfg["subjectCode"], code, new_a_cfg, [], plan
                )
            else:
                changed_fields = self._diff_assignment(new_a_cfg, prev_assignments_cfg[code])
                # Content file diff
                self._collect_new_content_files(
                    tmp_dir, new_cfg["subjectCode"], code, new_a_cfg,
                    prev_assignments_cfg[code].get("contentFiles", []), plan
                )
                # Old content files no longer referenced
                for old_entry in prev_assignments_cfg[code].get("contentFiles", []):
                    old_fn = old_entry.get("filename")
                    new_fns = {e.get("filename") for e in new_a_cfg.get("contentFiles", [])}
                    if old_fn and old_fn not in new_fns:
                        old_key = f"subjects/{new_cfg['subjectCode']}/assignments/{code}/{old_fn}"
                        plan.removed_s3_keys.append(old_key)

                if changed_fields:
                    plan.assignments_to_update.append((code, changed_fields))

        for code in prev_assignments_cfg:
            if code not in new_assignments_cfg:
                plan.assignments_to_delete.append(code)

        # If subject is being created, subject_action is "create" regardless of field diff
        if subject is not None and plan.subject_action == "update":
            # If nothing changed at all, mark as none so we still store the new version
            # but skip redundant DB writes
            if (
                not plan.subject_fields_changed
                and not plan.assignments_to_create
                and not plan.assignments_to_update
                and not plan.assignments_to_delete
            ):
                plan.subject_action = "none"

        return plan

    def _diff_assignment(
        self,
        new_a: dict[str, Any],
        prev_a: dict[str, Any],
    ) -> list[str]:
        changed: list[str] = []

        simple_fields = ["title", "description", "deadline", "min_grade", "max_grade"]
        for f in simple_fields:
            if new_a.get(f) != prev_a.get(f):
                changed.append(f)

        # Config JSONB fields
        config_keys = ["review_mode", "late_policy", "max_submissions", "download_links",
                       "variants_required", "sandbox", "variants"]
        new_config = {k: new_a[k] for k in config_keys if k in new_a}
        prev_config = {k: prev_a[k] for k in config_keys if k in prev_a}
        if new_config != prev_config:
            changed.append("config")

        # Content files list
        if new_a.get("contentFiles") != prev_a.get("contentFiles"):
            changed.append("content_files")

        return changed

    def _collect_new_content_files(
        self,
        tmp_dir: Path,
        subject_code: str,
        assignment_code: str,
        a_cfg: dict[str, Any],
        prev_content_files: list[dict[str, Any]],
        plan: ConfigApplyPlan,
    ) -> None:
        prev_filenames = {e.get("filename") for e in prev_content_files}
        for entry in a_cfg.get("contentFiles", []):
            filename: str | None = entry.get("filename")
            if not filename or filename in prev_filenames:
                continue
            local_path = tmp_dir / "assignments" / assignment_code / filename
            if local_path.exists():
                s3_key = f"subjects/{subject_code}/assignments/{assignment_code}/{filename}"
                plan.new_s3_files.append((local_path, s3_key))

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_plan(self, subject_code: str, plan: ConfigApplyPlan) -> None:
        logger.info(
            "config_apply_plan",
            subject_code=subject_code,
            subject_action=plan.subject_action,
            subject_fields_changed=plan.subject_fields_changed,
            new_s3_files=[str(p) for _, p in plan.new_s3_files],
            removed_s3_keys=plan.removed_s3_keys,
            assignments_to_create=plan.assignments_to_create,
            assignments_to_update=[(c, f) for c, f in plan.assignments_to_update],
            assignments_to_delete=plan.assignments_to_delete,
        )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def _execute_plan(
        self,
        plan: ConfigApplyPlan,
        new_cfg: dict[str, Any],
        zip_bytes: bytes,
        sha256: str,
        subject: Subject | None,
        owner_id: int,
        subject_code: str,
        db: AsyncSession,
        tmp_dir: Path,
    ) -> ApplyResult:
        # Step 1: upload new S3 files, collect {s3_key: url}
        url_map: dict[str, str] = {}
        if self._storage is not None and plan.new_s3_files:
            for local_path, s3_key in plan.new_s3_files:
                try:
                    url = await self._storage.upload_file(local_path, s3_key)
                    url_map[s3_key] = url
                    logger.info("config_apply_s3_uploaded", key=s3_key)
                except Exception as exc:
                    logger.error("config_apply_s3_upload_failed", key=s3_key, error=str(exc))
                    raise RuntimeError(f"S3 upload failed for {s3_key}: {exc}") from exc

        # Step 2-6: DB transaction
        if subject is None:
            # Create new subject
            subject = Subject(
                code=subject_code,
                name=new_cfg.get("name", subject_code),
                description=new_cfg.get("description") or None,
                github_repo=new_cfg.get("githubRepo") or None,
                owner_id=owner_id,
                status=SubjectStatus.ACTIVE,
            )
            db.add(subject)
            await db.flush()
            subject_created = True
        else:
            subject_created = False
            # Apply only changed subject fields
            self._apply_subject_fields(plan, new_cfg, subject, url_map, subject_code)

        # Apply image URLs for new uploads (if subject was just created)
        if subject_created:
            for cfg_key, attr in (("gridPicture", "grid_picture_url"), ("mainPicture", "main_picture_url")):
                filename: str | None = new_cfg.get(cfg_key)
                if filename:
                    s3_key = f"subjects/{subject_code}/images/{filename}"
                    if s3_key in url_map:
                        setattr(subject, attr, url_map[s3_key])

        # Process assignments
        new_assignments_cfg: dict[str, Any] = new_cfg.get("assignments", {})

        for code in plan.assignments_to_create:
            a_cfg = new_assignments_cfg[code]
            sa = await self._create_assignment(db, subject, code, a_cfg, url_map, subject_code)
            await db.flush()
            # Create StudentAssignment rows for all currently enrolled students
            enrolled = await db.execute(
                select(SubjectsStudents.student_id).where(
                    SubjectsStudents.subject_id == subject.id
                )
            )
            for (student_id,) in enrolled:
                db.add(StudentAssignment(student_id=student_id, subjects_assignment_id=sa.id))

        for code, changed_fields in plan.assignments_to_update:
            a_cfg = new_assignments_cfg[code]
            result = await db.execute(
                select(SubjectsAssignment).where(
                    SubjectsAssignment.subject_id == subject.id,
                    SubjectsAssignment.code == code,
                )
            )
            sa = result.scalar_one_or_none()
            if sa is not None:
                self._apply_assignment_fields(changed_fields, sa, a_cfg, url_map, subject_code, code)

        for code in plan.assignments_to_delete:
            result = await db.execute(
                select(SubjectsAssignment).where(
                    SubjectsAssignment.subject_id == subject.id,
                    SubjectsAssignment.code == code,
                )
            )
            sa = result.scalar_one_or_none()
            if sa is not None:
                await db.delete(sa)

        # Insert new SubjectPluginConfig version
        version = await self._next_version(db, subject.id)
        db.add(SubjectPluginConfig(
            subject_id=subject.id,
            version=version,
            content_hash=sha256,
            config=new_cfg,
            zip_data=zip_bytes,
            loaded_from=None,
        ))

        await db.commit()
        logger.info(
            "config_apply_committed",
            subject_code=subject_code,
            version=version,
            subject_action="created" if subject_created else plan.subject_action,
        )

        # Step 7: best-effort S3 cleanup
        if self._storage is not None:
            for key in plan.removed_s3_keys:
                try:
                    await self._storage.delete_file(key)
                    logger.info("config_apply_s3_deleted", key=key)
                except Exception as exc:
                    logger.warning("config_apply_s3_delete_failed", key=key, error=str(exc))

        action = "created" if subject_created else ("updated" if plan.subject_action != "none" else "unchanged")
        return ApplyResult(changed=True, subject_action=action, subject_name=subject.name)

    def _apply_subject_fields(
        self,
        plan: ConfigApplyPlan,
        new_cfg: dict[str, Any],
        subject: Subject,
        url_map: dict[str, str],
        subject_code: str,
    ) -> None:
        field_to_cfg: dict[str, Any] = {
            "name": new_cfg.get("name", subject_code),
            "description": new_cfg.get("description") or None,
            "github_repo": new_cfg.get("githubRepo") or None,
        }
        for f in plan.subject_fields_changed:
            if f in field_to_cfg:
                setattr(subject, f, field_to_cfg[f])
            elif f == "grid_picture_url":
                filename = new_cfg.get("gridPicture")
                if filename:
                    s3_key = f"subjects/{subject_code}/images/{filename}"
                    if s3_key in url_map:
                        subject.grid_picture_url = url_map[s3_key]
            elif f == "main_picture_url":
                filename = new_cfg.get("mainPicture")
                if filename:
                    s3_key = f"subjects/{subject_code}/images/{filename}"
                    if s3_key in url_map:
                        subject.main_picture_url = url_map[s3_key]

    def _apply_assignment_fields(
        self,
        changed_fields: list[str],
        sa: SubjectsAssignment,
        a_cfg: dict[str, Any],
        url_map: dict[str, str],
        subject_code: str,
        assignment_code: str,
    ) -> None:
        for f in changed_fields:
            if f == "title":
                sa.title = a_cfg.get("title", assignment_code)
            elif f == "description":
                sa.description = a_cfg.get("description") or None
            elif f == "deadline":
                sa.deadline = self._parse_deadline(a_cfg.get("deadline"))
            elif f == "min_grade":
                sa.min_grade = a_cfg.get("min_grade", 0)
            elif f == "max_grade":
                sa.max_grade = a_cfg.get("max_grade", 100)
            elif f == "config":
                sa.config = self._build_assignment_config(a_cfg)
            elif f == "content_files":
                sa.content_files = self._build_content_files(a_cfg, url_map, subject_code, assignment_code)

    async def _create_assignment(
        self,
        db: AsyncSession,
        subject: Subject,
        code: str,
        a_cfg: dict[str, Any],
        url_map: dict[str, str],
        subject_code: str,
    ) -> SubjectsAssignment:
        content_files = self._build_content_files(a_cfg, url_map, subject_code, code)
        sa = SubjectsAssignment(
            subject_id=subject.id,
            code=code,
            title=a_cfg.get("title", code),
            description=a_cfg.get("description") or None,
            deadline=self._parse_deadline(a_cfg.get("deadline")),
            min_grade=a_cfg.get("min_grade", 0),
            max_grade=a_cfg.get("max_grade", 100),
            config=self._build_assignment_config(a_cfg),
            content_files=content_files or None,
        )
        db.add(sa)
        return sa

    def _build_assignment_config(self, a_cfg: dict[str, Any]) -> dict[str, Any]:
        config: dict[str, Any] = {}
        for key in (
            "review_mode", "late_policy", "max_submissions", "download_links",
            "variants_required", "sandbox", "variants",
        ):
            if key in a_cfg:
                config[key] = a_cfg[key]
        return config

    def _build_content_files(
        self,
        a_cfg: dict[str, Any],
        url_map: dict[str, str],
        subject_code: str,
        assignment_code: str,
    ) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for entry in a_cfg.get("contentFiles", []):
            filename: str | None = entry.get("filename")
            display_name: str = entry.get("displayName", filename or "")
            if not filename:
                continue
            s3_key = f"subjects/{subject_code}/assignments/{assignment_code}/{filename}"
            url = url_map.get(s3_key, "")
            result.append({"url": url, "display_name": display_name, "filename": filename})
        return result

    @staticmethod
    def _parse_deadline(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value)).replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return None

    async def _next_version(self, db: AsyncSession, subject_id: int) -> int:
        result = await db.execute(
            select(func.coalesce(func.max(SubjectPluginConfig.version), 0)).where(
                SubjectPluginConfig.subject_id == subject_id
            )
        )
        return (result.scalar_one() or 0) + 1
