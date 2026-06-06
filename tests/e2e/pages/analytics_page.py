"""Page object for the analytics dashboard."""

from __future__ import annotations

from playwright.sync_api import Page, expect


class AnalyticsPage:
    def __init__(self, page: Page, app_url: str) -> None:
        self.page = page
        self.app_url = app_url

    def navigate(self) -> None:
        self.page.goto(f"{self.app_url}/teacher/analytics")

    def assert_on_analytics(self) -> None:
        expect(self.page).to_have_url(f"{self.app_url}/teacher/analytics")

    def assert_stats_visible(self) -> None:
        self.page.wait_for_load_state("networkidle")
        # The analytics dashboard shows several stat cards
        expect(self.page.locator("h1, h2").first).to_be_visible()
        # Page should not have server error
        assert "Internal Server Error" not in (self.page.content() or "")
        assert "500" not in self.page.title()

    def assert_no_error(self) -> None:
        content = self.page.content()
        assert "Internal Server Error" not in content
        assert "500" not in self.page.title()
