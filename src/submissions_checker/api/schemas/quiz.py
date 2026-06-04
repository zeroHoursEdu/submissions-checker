"""Pydantic schemas for quiz export/import validation."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from submissions_checker.db.models.enums import QuizQuestionType


class QuizConfigSchema(BaseModel):
    total_questions: int | None = None
    time_limit_minutes: int | None = None
    max_quiz_attempts: int | None = None
    pass_threshold_pct: float = 0.6
    shuffle_questions: bool = True
    shuffle_options: bool = True
    show_correct_answers_after: bool = False


class QuizQuestionSchema(BaseModel):
    type: QuizQuestionType
    text: str
    points: int = 1
    is_required: bool = False
    sort_order: int = 0
    config: dict[str, Any]


class QuizExportSchema(BaseModel):
    schema_version: int = 1
    exported_at: str
    assignment_title: str
    config: QuizConfigSchema
    questions: list[QuizQuestionSchema]
