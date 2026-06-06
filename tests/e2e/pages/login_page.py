"""Page object for the login page."""

from __future__ import annotations

from playwright.sync_api import Page, expect


class LoginPage:
    def __init__(self, page: Page, app_url: str) -> None:
        self.page = page
        self.app_url = app_url

    def navigate(self) -> None:
        self.page.goto(f"{self.app_url}/auth/login")

    def login(self, username: str, password: str) -> None:
        self.page.fill("#username", username)
        self.page.fill("#password", password)
        self.page.locator('form[action="/auth/login"] button[type="submit"]').click()

    def assert_on_teacher_dashboard(self) -> None:
        expect(self.page).to_have_url(f"{self.app_url}/teacher")

    def assert_on_student_portal(self) -> None:
        self.page.wait_for_url(f"{self.app_url}/portal**")

    def assert_login_error(self) -> None:
        expect(self.page.locator(".bg-red-50")).to_be_visible()
        assert "/auth/login" in self.page.url

    def logout(self) -> None:
        self.page.goto(f"{self.app_url}/auth/logout")
