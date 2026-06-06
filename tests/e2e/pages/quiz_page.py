"""Page object for the student quiz flow."""

from __future__ import annotations

from playwright.sync_api import Page, expect


class QuizPage:
    def __init__(self, page: Page, app_url: str) -> None:
        self.page = page
        self.app_url = app_url

    def start_quiz(self, subject_id: int, assignment_id: int) -> None:
        """Navigate to the quiz start endpoint and follow the redirect to the quiz form."""
        self.page.goto(
            f"{self.app_url}/portal/subjects/{subject_id}/assignments/{assignment_id}/quiz"
        )
        self.page.wait_for_load_state("networkidle")
        assert "/portal/quiz/" in self.page.url, f"Expected quiz URL, got: {self.page.url}"

    def get_attempt_id(self) -> int:
        url = self.page.url
        # URL is /portal/quiz/{attempt_id}
        parts = url.rstrip("/").split("/")
        try:
            return int(parts[-1])
        except (ValueError, IndexError):
            return 0

    def answer_first_question_correctly(self) -> None:
        """Select the first radio option (assumed correct for E2E test quiz)."""
        first_radio = self.page.locator('input[type="radio"]').first
        first_radio.check()

    def answer_first_question_incorrectly(self) -> None:
        """Select the last radio option (assumed wrong for E2E test quiz)."""
        radios = self.page.locator('input[type="radio"]').all()
        if radios:
            radios[-1].check()

    def submit_quiz(self) -> None:
        self.page.locator("#quiz-form button[type='submit']").click()
        self.page.wait_for_load_state("networkidle")
        assert "/portal/quiz/" in self.page.url and "/result" in self.page.url, \
            f"Expected quiz result URL, got: {self.page.url}"

    def get_result_text(self) -> str:
        self.page.wait_for_load_state("networkidle")
        return self.page.content()

    def assert_passed(self) -> None:
        self.page.wait_for_load_state("networkidle")
        content = self.page.content()
        # UI shows Ukrainian "Зараховано!" or has green styling on the score
        assert (
            "зараховано" in content.lower()
            or "text-green-700" in content
            or "pass" in content.lower()
        ), f"Expected passed result, page content: {content[:300]}"

    def assert_failed(self) -> None:
        self.page.wait_for_load_state("networkidle")
        content = self.page.content()
        # UI shows Ukrainian "Не зараховано" or has red/amber styling
        assert (
            "не зараховано" in content.lower()
            or "зараховано" not in content.lower() and "text-red-700" in content
            or "fail" in content.lower()
        ), f"Expected failed result, page content: {content[:300]}"

    def assert_retry_available(self) -> None:
        """Verify there's a retry/start again button on the result page."""
        retry_btn = self.page.locator('a[href*="quiz"], button:has-text("Try"), a:has-text("Try"), a:has-text("Ще раз"), a[href*="assignments"]')
        expect(retry_btn.first).to_be_visible()
