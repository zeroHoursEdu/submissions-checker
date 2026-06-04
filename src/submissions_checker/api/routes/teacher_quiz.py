"""Teacher quiz management routes — CRUD for quiz templates and questions."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from submissions_checker.api.dependencies import DBSession, TeacherUser
from submissions_checker.api.schemas.quiz import QuizExportSchema
from submissions_checker.db.models import QuizQuestion, QuizTemplate, SubjectsAssignment
from submissions_checker.db.models.enums import QuizQuestionType

router = APIRouter(
    prefix="/teacher/subjects/{subject_id}/assignments/{sa_id}/quiz",
    tags=["teacher-quiz"],
)
templates = Jinja2Templates(directory="templates")

_QUESTION_TYPES = [t.value for t in QuizQuestionType]


async def _get_assignment(subject_id: int, sa_id: int, db: DBSession) -> SubjectsAssignment:  # type: ignore[valid-type]
    result = await db.execute(
        select(SubjectsAssignment)
        .where(SubjectsAssignment.id == sa_id, SubjectsAssignment.subject_id == subject_id)
        .options(selectinload(SubjectsAssignment.subject))
    )
    assignment = result.scalar_one_or_none()
    if assignment is None:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return assignment


async def _get_template(sa_id: int, db: DBSession) -> QuizTemplate | None:
    result = await db.execute(
        select(QuizTemplate)
        .where(QuizTemplate.subjects_assignment_id == sa_id)
        .options(selectinload(QuizTemplate.questions))
    )
    return result.scalar_one_or_none()


def _build_config(form: dict) -> dict:  # type: ignore[type-arg]
    raw_total = form.get("total_questions", "")
    raw_limit = form.get("time_limit_minutes", "")
    raw_max = form.get("max_quiz_attempts", "")
    raw_ac = form.get("anti_cheat", "")
    try:
        anti_cheat = json.loads(raw_ac) if raw_ac and raw_ac.strip() else {"rules": []}
    except json.JSONDecodeError:
        anti_cheat = {"rules": []}
    return {
        "total_questions": int(raw_total) if raw_total and raw_total.strip() else None,
        "time_limit_minutes": int(raw_limit) if raw_limit and raw_limit.strip() else None,
        "max_quiz_attempts": int(raw_max) if raw_max and raw_max.strip() else None,
        "pass_threshold_pct": float(form.get("pass_threshold_pct", 0.6)),
        "shuffle_questions": form.get("shuffle_questions") == "on",
        "shuffle_options": form.get("shuffle_options") == "on",
        "show_correct_answers_after": form.get("show_correct_answers_after") == "on",
        "anti_cheat": anti_cheat,
    }


@router.get("", response_class=HTMLResponse)
async def quiz_editor(
    request: Request,
    subject_id: int,
    sa_id: int,
    db: DBSession,
    current_user: TeacherUser,
) -> HTMLResponse:
    assignment = await _get_assignment(subject_id, sa_id, db)
    template = await _get_template(sa_id, db)
    return templates.TemplateResponse(
        request=request,
        name="teacher_quiz_editor.html",
        context={
            "current_user": current_user,
            "assignment": assignment,
            "subject_id": subject_id,
            "template": template,
            "questions": template.questions if template else [],
            "question_types": _QUESTION_TYPES,
        },
    )


@router.post("/config")
async def upsert_quiz_config(
    request: Request,
    subject_id: int,
    sa_id: int,
    db: DBSession,
    current_user: TeacherUser,
) -> RedirectResponse:
    await _get_assignment(subject_id, sa_id, db)
    form = dict(await request.form())
    config = _build_config(form)

    template = await _get_template(sa_id, db)
    if template is None:
        template = QuizTemplate(subjects_assignment_id=sa_id, version=1, config=config)
        db.add(template)
    else:
        template.config = config
        template.version += 1

    await db.commit()
    return RedirectResponse(
        url=f"/teacher/subjects/{subject_id}/assignments/{sa_id}/quiz",
        status_code=303,
    )


@router.post("/questions")
async def add_question(
    request: Request,
    subject_id: int,
    sa_id: int,
    db: DBSession,
    current_user: TeacherUser,
) -> RedirectResponse:
    await _get_assignment(subject_id, sa_id, db)
    template = await _get_template(sa_id, db)
    if template is None:
        raise HTTPException(status_code=400, detail="Create quiz config first")

    form = dict(await request.form())
    q_type = form.get("type", "")
    if q_type not in _QUESTION_TYPES:
        raise HTTPException(status_code=422, detail="Invalid question type")

    try:
        config = json.loads(form.get("config", "{}"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="Invalid config JSON")

    question = QuizQuestion(
        template_id=template.id,
        type=QuizQuestionType(q_type),
        text=form.get("text", "").strip(),
        points=int(form.get("points", 1)),
        is_required=form.get("is_required") == "on",
        sort_order=int(form.get("sort_order", len(template.questions))),
        config=config,
    )
    db.add(question)
    template.version += 1
    await db.commit()
    return RedirectResponse(
        url=f"/teacher/subjects/{subject_id}/assignments/{sa_id}/quiz",
        status_code=303,
    )


@router.post("/questions/{q_id}/update")
async def update_question(
    request: Request,
    subject_id: int,
    sa_id: int,
    q_id: int,
    db: DBSession,
    current_user: TeacherUser,
) -> RedirectResponse:
    await _get_assignment(subject_id, sa_id, db)
    template = await _get_template(sa_id, db)
    if template is None:
        raise HTTPException(status_code=404, detail="Quiz template not found")

    question = await db.get(QuizQuestion, q_id)
    if question is None or question.template_id != template.id:
        raise HTTPException(status_code=404, detail="Question not found")

    form = dict(await request.form())
    q_type = form.get("type", question.type.value)
    if q_type not in _QUESTION_TYPES:
        raise HTTPException(status_code=422, detail="Invalid question type")

    try:
        config = json.loads(form.get("config", "{}"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="Invalid config JSON")

    question.type = QuizQuestionType(q_type)
    question.text = form.get("text", question.text).strip()
    question.points = int(form.get("points", question.points))
    question.is_required = form.get("is_required") == "on"
    question.sort_order = int(form.get("sort_order", question.sort_order))
    question.config = config
    template.version += 1
    await db.commit()
    return RedirectResponse(
        url=f"/teacher/subjects/{subject_id}/assignments/{sa_id}/quiz",
        status_code=303,
    )


@router.post("/questions/{q_id}/delete")
async def delete_question(
    subject_id: int,
    sa_id: int,
    q_id: int,
    db: DBSession,
    current_user: TeacherUser,
) -> RedirectResponse:
    await _get_assignment(subject_id, sa_id, db)
    template = await _get_template(sa_id, db)
    if template is None:
        raise HTTPException(status_code=404, detail="Quiz template not found")

    question = await db.get(QuizQuestion, q_id)
    if question is None or question.template_id != template.id:
        raise HTTPException(status_code=404, detail="Question not found")

    await db.delete(question)
    template.version += 1
    await db.commit()
    return RedirectResponse(
        url=f"/teacher/subjects/{subject_id}/assignments/{sa_id}/quiz",
        status_code=303,
    )


@router.get("/export")
async def export_quiz(
    subject_id: int,
    sa_id: int,
    db: DBSession,
    current_user: TeacherUser,
) -> Response:
    assignment = await _get_assignment(subject_id, sa_id, db)
    template = await _get_template(sa_id, db)
    if template is None:
        raise HTTPException(status_code=404, detail="No quiz template for this assignment")

    payload = {
        "schema_version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "assignment_title": assignment.title,
        "config": template.config,
        "questions": [
            {
                "type": q.type.value,
                "text": q.text,
                "points": q.points,
                "is_required": q.is_required,
                "sort_order": q.sort_order,
                "config": q.config,
            }
            for q in template.questions
        ],
    }
    return Response(
        content=json.dumps(payload, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="quiz_{sa_id}.json"'},
    )


@router.post("/import")
async def import_quiz(
    subject_id: int,
    sa_id: int,
    db: DBSession,
    current_user: TeacherUser,
    file: UploadFile,
) -> RedirectResponse:
    await _get_assignment(subject_id, sa_id, db)

    content = await file.read()
    try:
        raw = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="Invalid JSON file")

    if raw.get("schema_version") != 1:
        raise HTTPException(status_code=422, detail="Unsupported schema_version")

    try:
        export = QuizExportSchema.model_validate(raw)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid quiz format: {exc}")

    # Delete existing template atomically (cascade deletes questions)
    existing = await _get_template(sa_id, db)
    if existing is not None:
        await db.delete(existing)
        await db.flush()

    template = QuizTemplate(
        subjects_assignment_id=sa_id,
        version=1,
        config=export.config.model_dump(),
    )
    db.add(template)
    await db.flush()

    for q in export.questions:
        question = QuizQuestion(
            template_id=template.id,
            type=q.type,
            text=q.text,
            points=q.points,
            is_required=q.is_required,
            sort_order=q.sort_order,
            config=q.config,
        )
        db.add(question)

    await db.commit()
    return RedirectResponse(
        url=f"/teacher/subjects/{subject_id}/assignments/{sa_id}/quiz",
        status_code=303,
    )
