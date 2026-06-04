"""Pydantic schemas for the student-facing portal."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from submissions_checker.db.models.enums import SubmissionStatus


class StudentListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str
    github_username: str
    group_name: str


class ContentFile(BaseModel):
    url: str
    display_name: str
    filename: str


class SubjectCard(BaseModel):
    id: int
    name: str
    description: str | None
    total_assignments: int
    done_assignments: int
    grid_picture_url: str | None = None


class AssignmentRow(BaseModel):
    student_assignment_id: int
    title: str
    deadline: datetime | None
    grade: int | None
    min_grade: int
    max_grade: int
    submission_status: SubmissionStatus | None


class AssignmentDetail(BaseModel):
    student_assignment_id: int
    title: str
    description: str | None
    deadline: datetime | None
    grade: int | None
    min_grade: int
    max_grade: int
    config: dict  # type: ignore[type-arg]
    submission_status: SubmissionStatus | None
    submission_id: int | None
    latest_submission_created_at: datetime | None
    quiz_attempt_id: int | None
    quiz_attempts_used: int = 0
    quiz_max_attempts: int | None = None
    check_reason: str | None = None
    content_files: list[ContentFile] = []
