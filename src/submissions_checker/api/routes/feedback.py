"""Public (no-auth) feedback form routes — accessed via single-use token links."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from submissions_checker.api.dependencies import DBSession
from submissions_checker.db.models.feedback_response import FeedbackResponse
from submissions_checker.db.models.feedback_token import FeedbackToken
from submissions_checker.db.models.subject import Subject
from submissions_checker.core.templates import render

router = APIRouter(tags=["feedback"])


@router.get("/feedback/{token}", response_class=HTMLResponse)
async def feedback_form(token: str, request: Request, db: DBSession) -> HTMLResponse:
    token_result = await db.execute(
        select(FeedbackToken).where(FeedbackToken.token == token)
    )
    feedback_token = token_result.scalar_one_or_none()

    if feedback_token is None:
        return render(request, "feedback_not_found.html", {}, status_code=404)

    if feedback_token.used_at is not None:
        return render(request, "feedback_already_submitted.html", {})

    from submissions_checker.db.models.feedback_request import FeedbackRequest
    fr_result = await db.execute(
        select(FeedbackRequest).where(FeedbackRequest.id == feedback_token.feedback_request_id)
    )
    feedback_request = fr_result.scalar_one()
    subject_result = await db.execute(select(Subject).where(Subject.id == feedback_request.subject_id))
    subject = subject_result.scalar_one()

    return render(request, "feedback_form.html", {"token": token, "subject": subject})


@router.post("/feedback/{token}", response_model=None)
async def submit_feedback(
    token: str,
    request: Request,
    db: DBSession,
    rating: int = Form(...),
    went_well: str = Form(...),
    went_bad: str = Form(...),
    to_change: str = Form(...),
) -> HTMLResponse | RedirectResponse:
    token_result = await db.execute(
        select(FeedbackToken).where(FeedbackToken.token == token)
    )
    feedback_token = token_result.scalar_one_or_none()

    if feedback_token is None:
        return render(request, "feedback_not_found.html", {}, status_code=404)

    if feedback_token.used_at is not None:
        return render(request, "feedback_already_submitted.html", {})

    if rating < 1 or rating > 5:
        from submissions_checker.db.models.feedback_request import FeedbackRequest
        fr_result = await db.execute(
            select(FeedbackRequest).where(FeedbackRequest.id == feedback_token.feedback_request_id)
        )
        feedback_request = fr_result.scalar_one()
        subject_result = await db.execute(select(Subject).where(Subject.id == feedback_request.subject_id))
        subject = subject_result.scalar_one()
        return render(request, "feedback_form.html", {"token": token, "subject": subject, "error": "Rating must be between 1 and 5."}, status_code=422)

    from submissions_checker.db.models.feedback_request import FeedbackRequest
    fr_result = await db.execute(
        select(FeedbackRequest).where(FeedbackRequest.id == feedback_token.feedback_request_id)
    )
    feedback_request = fr_result.scalar_one()

    db.add(FeedbackResponse(
        feedback_token_id=feedback_token.id,
        subject_id=feedback_request.subject_id,
        rating=rating,
        went_well=went_well.strip(),
        went_bad=went_bad.strip(),
        to_change=to_change.strip(),
        submitted_at=datetime.now(UTC),
    ))
    feedback_token.used_at = datetime.now(UTC)
    await db.commit()

    return RedirectResponse(url=f"/feedback/{token}/thanks", status_code=303)


@router.get("/feedback/{token}/thanks", response_class=HTMLResponse)
async def feedback_thanks(token: str, request: Request) -> HTMLResponse:
    return render(request, "feedback_thank_you.html", {})
