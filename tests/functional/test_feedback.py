"""Functional coverage for the PUBLIC, unauthenticated FEEDBACK token flow.

These routes (``api/routes/feedback.py``) are reached by anyone holding a
single-use token link — there is no login. The security properties under test:

* an invalid/unknown token is rejected (404);
* a valid token renders the form;
* submitting records exactly one FeedbackResponse and consumes the token
  (``used_at`` is set);
* the token is SINGLE-USE: a second submission with the same token is rejected
  and creates no further response.

NOTE: ``FeedbackToken`` has no expiry column (only ``used_at``), so there is no
"expired token" code path to test — single-use consumption is the only lifecycle
gate. This is asserted by checking the model fields directly below.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from submissions_checker.db.models.feedback_request import FeedbackRequest
from submissions_checker.db.models.feedback_response import FeedbackResponse
from submissions_checker.db.models.feedback_token import FeedbackToken
from submissions_checker.db.models.semester import Semester
from submissions_checker.db.models.subject import Subject

pytestmark = pytest.mark.asyncio


# Guard the NOTE above: if an expiry column is ever added, this test fails loudly
# so the suite gets an expiry-rejection case.
async def test_feedback_token_has_no_expiry_column() -> None:
    assert not hasattr(FeedbackToken, "expires_at")


# ── Arrangement helpers ──────────────────────────────────────────────────────


async def _make_feedback_token(
    db, *, teacher_id: int, token: str = "tok-valid-123", used_at: datetime | None = None
) -> FeedbackToken:
    """Build the full Subject → Semester → FeedbackRequest → Token chain."""
    subject = Subject(name="Algorithms")
    db.add(subject)
    semester = Semester(
        name="2026 Spring",
        season="SPRING",
        start_date=date(2026, 2, 1),
        end_date=date(2026, 6, 1),
    )
    db.add(semester)
    await db.commit()
    await db.refresh(subject)
    await db.refresh(semester)

    request = FeedbackRequest(
        subject_id=subject.id,
        semester_id=semester.id,
        created_by_teacher_id=teacher_id,
    )
    db.add(request)
    await db.commit()
    await db.refresh(request)

    # A student the token is addressed to.
    from submissions_checker.db.models.group import Group
    from submissions_checker.db.models.student import Student

    group = Group(name="G-fb")
    db.add(group)
    await db.commit()
    await db.refresh(group)
    student = Student(group_id=group.id, email="fb@example.com", full_name="FB Student")
    db.add(student)
    await db.commit()
    await db.refresh(student)

    feedback_token = FeedbackToken(
        feedback_request_id=request.id,
        student_id=student.id,
        token=token,
        used_at=used_at,
    )
    db.add(feedback_token)
    await db.commit()
    await db.refresh(feedback_token)
    return feedback_token


_VALID_FORM = {
    "rating": "4",
    "went_well": "lectures",
    "went_bad": "pacing",
    "to_change": "more labs",
}


# ── GET form ──────────────────────────────────────────────────────────────────


async def test_get_form_valid_token_renders(client: AsyncClient, teacher, db) -> None:
    await _make_feedback_token(db, teacher_id=teacher.id)
    resp = await client.get("/feedback/tok-valid-123")
    assert resp.status_code == 200


async def test_get_form_unknown_token_404(client: AsyncClient) -> None:
    resp = await client.get("/feedback/does-not-exist")
    assert resp.status_code == 404


async def test_get_form_already_used_token_shows_already_submitted(
    client: AsyncClient, teacher, db
) -> None:
    await _make_feedback_token(
        db, teacher_id=teacher.id, token="tok-used", used_at=datetime.now(UTC)
    )
    resp = await client.get("/feedback/tok-used")
    # Handler returns the "already submitted" page with a 200 (not a 404).
    assert resp.status_code == 200


# ── POST submission + single-use consumption ──────────────────────────────────


async def test_post_records_response_and_consumes_token(client: AsyncClient, teacher, db) -> None:
    feedback_token = await _make_feedback_token(db, teacher_id=teacher.id)

    resp = await client.post("/feedback/tok-valid-123", data=_VALID_FORM, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/feedback/tok-valid-123/thanks"

    # Exactly one FeedbackResponse recorded, with the submitted values.
    responses = (
        (
            await db.execute(
                select(FeedbackResponse).where(
                    FeedbackResponse.feedback_token_id == feedback_token.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(responses) == 1
    assert responses[0].rating == 4
    assert responses[0].went_well == "lectures"

    # Token is consumed.
    fresh = await db.get(FeedbackToken, feedback_token.id)
    await db.refresh(fresh)
    assert fresh.used_at is not None


async def test_post_unknown_token_404_records_nothing(client: AsyncClient, db) -> None:
    resp = await client.post("/feedback/nope", data=_VALID_FORM, follow_redirects=False)
    assert resp.status_code == 404
    count = (await db.execute(select(FeedbackResponse))).scalars().all()
    assert count == []


async def test_post_invalid_rating_rejected(client: AsyncClient, teacher, db) -> None:
    await _make_feedback_token(db, teacher_id=teacher.id)
    bad = {**_VALID_FORM, "rating": "9"}
    resp = await client.post("/feedback/tok-valid-123", data=bad, follow_redirects=False)
    assert resp.status_code == 422
    # No response stored and token NOT consumed.
    assert (await db.execute(select(FeedbackResponse))).scalars().all() == []
    fresh = (
        await db.execute(select(FeedbackToken).where(FeedbackToken.token == "tok-valid-123"))
    ).scalar_one()
    assert fresh.used_at is None


async def test_token_is_single_use(client: AsyncClient, teacher, db) -> None:
    feedback_token = await _make_feedback_token(db, teacher_id=teacher.id)

    first = await client.post("/feedback/tok-valid-123", data=_VALID_FORM, follow_redirects=False)
    assert first.status_code == 303

    # Second submission with the same (now-consumed) token is refused: it returns
    # the "already submitted" page (200) and does NOT create a second response.
    second = await client.post(
        "/feedback/tok-valid-123",
        data={**_VALID_FORM, "went_well": "SHOULD NOT BE STORED"},
        follow_redirects=False,
    )
    assert second.status_code == 200

    responses = (
        (
            await db.execute(
                select(FeedbackResponse).where(
                    FeedbackResponse.feedback_token_id == feedback_token.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(responses) == 1
    assert "SHOULD NOT BE STORED" not in responses[0].went_well


# ── Thanks page ───────────────────────────────────────────────────────────────


async def test_thanks_page_renders(client: AsyncClient) -> None:
    # Thanks page is a static render and does not validate the token.
    resp = await client.get("/feedback/anything/thanks")
    assert resp.status_code == 200
