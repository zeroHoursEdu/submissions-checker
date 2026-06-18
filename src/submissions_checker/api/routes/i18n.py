"""Language selection endpoint."""

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import RedirectResponse

from submissions_checker.core.i18n import AVAILABLE_LANGUAGES

router = APIRouter()

_YEAR_SECONDS = 365 * 24 * 3600


def _safe_redirect_target(referer: str | None) -> str:
    """Only allow same-site relative paths; reject open-redirect targets.

    A valid target must start with a single "/" and must not be a
    protocol-relative URL ("//host"). Anything else falls back to "/".
    """
    if referer and referer.startswith("/") and not referer.startswith("//"):
        return referer
    return "/"


@router.post("/set-language")
async def set_language(
    request: Request,
    lang: str = Form(...),
) -> Response:
    registered_codes = {l["code"] for l in AVAILABLE_LANGUAGES}
    if lang not in registered_codes:
        return Response(status_code=400, content="Unknown language code")

    redirect_to = _safe_redirect_target(request.headers.get("referer"))
    response = RedirectResponse(url=redirect_to, status_code=302)
    response.set_cookie(
        key="lang",
        value=lang,
        max_age=_YEAR_SECONDS,
        samesite="lax",
        httponly=True,
        path="/",
    )
    return response
