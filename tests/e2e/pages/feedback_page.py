"""Page objects for feedback flows (teacher requests, student responds)."""

from __future__ import annotations

from playwright.sync_api import Page, expect


class TeacherFeedbackPage:
    def __init__(self, page: Page, app_url: str) -> None:
        self.page = page
        self.app_url = app_url

    def navigate_to_subject_feedback(self, subject_id: int) -> None:
        self.page.goto(f"{self.app_url}/teacher/subjects/{subject_id}/feedback")

    def request_feedback_for_subject(self, subject_id: int) -> None:
        """Click the 'Request Feedback' button on the subject page."""
        self.page.goto(f"{self.app_url}/teacher/subjects/{subject_id}")
        self.page.wait_for_load_state("networkidle")
        form = self.page.locator(f'form[action="/teacher/subjects/{subject_id}/feedback/request"]')
        if form.count() > 0:
            form.locator('button[type="submit"]').click()
            self.page.wait_for_url(f"{self.app_url}/teacher/subjects/**")

    def assert_feedback_request_created(self) -> None:
        self.page.wait_for_load_state("networkidle")
        content = self.page.content()
        # Either a success flash or the disabled badge "Feedback sent"
        assert "feedback" in content.lower()

    def get_feedback_token_urls(self, subject_id: int) -> list[str]:
        """Scrape token URLs from the teacher feedback view page."""
        self.page.goto(f"{self.app_url}/teacher/subjects/{subject_id}/feedback")
        self.page.wait_for_load_state("networkidle")
        links = self.page.locator('a[href*="/feedback/"]').all()
        return [link.get_attribute("href") or "" for link in links]


class StudentFeedbackPage:
    def __init__(self, page: Page, app_url: str) -> None:
        self.page = page
        self.app_url = app_url

    def navigate_to_feedback_form(self, token: str) -> None:
        self.page.goto(f"{self.app_url}/feedback/{token}")

    def submit_feedback(
        self,
        rating: int = 5,
        went_well: str = "Everything went well in E2E test.",
        went_bad: str = "Nothing went badly in E2E test.",
        to_change: str = "Nothing to change in E2E test.",
    ) -> None:
        self.page.wait_for_load_state("networkidle")
        # Star inputs are display:none — set value via JS
        self.page.evaluate(
            f"document.querySelector('#star{rating}').checked = true;"
        )
        self.page.fill('textarea[name="went_well"]', went_well)
        self.page.fill('textarea[name="went_bad"]', went_bad)
        self.page.fill('textarea[name="to_change"]', to_change)
        self.page.locator('button[type="submit"]').click()

    def assert_submitted_successfully(self) -> None:
        self.page.wait_for_url(f"{self.app_url}/feedback/**/thanks**")
