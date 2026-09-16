"""Shared Jinja2 template renderer with automatic i18n context injection."""

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import DebugUndefined

from submissions_checker.core.config import get_settings
from submissions_checker.core.i18n import get_vocab

_settings = get_settings()
_undefined = DebugUndefined if _settings.is_development else None

_templates = Jinja2Templates(directory="templates")
if _undefined is not None:
    _templates.env.undefined = _undefined


def render(
    request: Request,
    template_name: str,
    context: dict[str, Any] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Render a template with vocab injected into context."""
    lang_cookie = request.cookies.get("lang")
    vocab = get_vocab(lang_cookie)

    merged: dict[str, Any] = {
        "vocab": vocab,
        # Lets a template show development-only affordances. Read from the same
        # setting the seeding migrations check, so a page can never advertise
        # accounts that this environment does not have.
        "is_development": _settings.is_development,
    }
    if context:
        merged.update(context)

    return _templates.TemplateResponse(
        request=request,
        name=template_name,
        context=merged,
        status_code=status_code,
    )
