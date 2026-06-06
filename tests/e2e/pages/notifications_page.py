"""Page object for student notification preferences."""

from __future__ import annotations

from playwright.sync_api import Page, expect


class NotificationsPage:
    def __init__(self, page: Page, app_url: str) -> None:
        self.page = page
        self.app_url = app_url

    def navigate(self) -> None:
        self.page.goto(f"{self.app_url}/portal/notification-preferences")

    def assert_on_page(self) -> None:
        expect(self.page).to_have_url(f"{self.app_url}/portal/notification-preferences")

    def _toggle_channel(self, case: str, method: str) -> None:
        """POST to the toggle endpoint directly."""
        response = self.page.request.post(
            f"{self.app_url}/portal/notification-preferences/{case}/{method}/toggle"
        )
        assert response.status in (200, 303), f"Toggle failed: {response.status}"
        self.page.goto(f"{self.app_url}/portal/notification-preferences")

    # Case values: SUBMISSION_CHECKED, FEEDBACK_REQUEST
    # Method values: EMAIL

    def toggle_email(self, case: str = "SUBMISSION_CHECKED") -> None:
        self._toggle_channel(case, "EMAIL")

    def get_toggle_state(self, case: str = "SUBMISSION_CHECKED", method: str = "EMAIL") -> str:
        """Return 'enabled' or 'disabled' by inspecting the button class on the preferences page."""
        self.navigate()
        self.page.wait_for_load_state("networkidle")
        selector = f'form[action*="{case}/{method}/toggle"] button'
        el = self.page.locator(selector).first
        if el.count() == 0:
            return "unknown"
        classes = el.get_attribute("class") or ""
        # Enabled buttons have bg-indigo-600; disabled ones have bg-slate-100
        return "enabled" if "bg-indigo-600" in classes else "disabled"
