"""Functional coverage for the I18N language-selection route (``/set-language``).

Behavior asserted against ``api/routes/i18n.py``:

* a registered language code sets an HTTP-only ``lang`` cookie and redirects;
* the redirect target is taken from a same-site ``Referer`` only (open-redirect
  guard), otherwise falls back to ``/``;
* an unregistered language code is rejected with 400 and sets no cookie.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from submissions_checker.core.i18n import AVAILABLE_LANGUAGES

pytestmark = pytest.mark.asyncio


def _a_valid_code() -> str:
    # The _load_i18n session fixture loads i18n/*.yml, so at least "uk" is present.
    assert AVAILABLE_LANGUAGES, "expected at least one registered language"
    return AVAILABLE_LANGUAGES[0]["code"]


async def test_set_language_valid_sets_cookie_and_redirects(
    client: AsyncClient,
) -> None:
    code = _a_valid_code()
    resp = await client.post(
        "/set-language", data={"lang": code}, follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"

    set_cookie = resp.headers["set-cookie"]
    assert "lang=" + code in set_cookie
    assert "HttpOnly" in set_cookie
    # Cookie is now attached to the client.
    assert client.cookies.get("lang") == code


async def test_set_language_honours_same_site_referer(client: AsyncClient) -> None:
    code = _a_valid_code()
    resp = await client.post(
        "/set-language",
        data={"lang": code},
        headers={"referer": "/subjects/3"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/subjects/3"


async def test_set_language_rejects_open_redirect_referer(
    client: AsyncClient,
) -> None:
    code = _a_valid_code()
    resp = await client.post(
        "/set-language",
        data={"lang": code},
        headers={"referer": "//evil.example.com/phish"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    # Protocol-relative URL is rejected; falls back to "/".
    assert resp.headers["location"] == "/"


async def test_set_language_unknown_code_400_no_cookie(client: AsyncClient) -> None:
    resp = await client.post(
        "/set-language", data={"lang": "zz-not-real"}, follow_redirects=False
    )
    assert resp.status_code == 400
    assert "set-cookie" not in resp.headers
    assert client.cookies.get("lang") is None
