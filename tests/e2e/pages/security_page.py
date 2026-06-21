"""Page object for permission / security probes.

These helpers navigate to protected or public URLs and expose the resulting
HTTP status and body so steps can assert the *real* authorization outcome
(401 / 403 / 404 / rejection page) rather than just URL changes — the app
returns raw JSON 401/403 from its dependency layer rather than redirecting
unauthenticated/under-privileged users to a login page.
"""

from __future__ import annotations

from playwright.sync_api import Page, Response


class SecurityPage:
    def __init__(self, page: Page, app_url: str) -> None:
        self.page = page
        self.app_url = app_url

    def goto(self, path: str) -> Response | None:
        """Navigate to a path and return the navigation Response (may be None
        only if navigation is aborted — never expected for these GETs)."""
        return self.page.goto(f"{self.app_url}{path}")

    def status_for(self, path: str) -> int:
        resp = self.goto(path)
        assert resp is not None, f"No HTTP response for navigation to {path}"
        return resp.status

    def body_text(self) -> str:
        """Return the visible text of the current document."""
        return self.page.inner_text("body")
