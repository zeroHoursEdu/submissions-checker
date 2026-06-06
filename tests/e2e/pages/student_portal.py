"""Page object for the student portal."""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Page, expect


class StudentPortal:
    def __init__(self, page: Page, app_url: str) -> None:
        self.page = page
        self.app_url = app_url

    def navigate(self) -> None:
        self.page.goto(f"{self.app_url}/portal")

    def assert_on_portal(self) -> None:
        self.page.wait_for_url(f"{self.app_url}/portal**")

    def open_subject(self, subject_name: str) -> None:
        self.page.get_by_text(subject_name).first.click()
        self.page.wait_for_url(f"{self.app_url}/portal/subjects/**")

    def open_assignment(self, subject_id: int, assignment_id: int) -> None:
        self.page.goto(f"{self.app_url}/portal/subjects/{subject_id}/assignments/{assignment_id}")

    def get_assignment_links(self) -> list[dict]:
        """Return all assignment links on the current subject page."""
        links = self.page.locator('a[href*="/assignments/"]').all()
        results = []
        for link in links:
            href = link.get_attribute("href") or ""
            parts = href.split("/")
            if len(parts) >= 6 and parts[-1].isdigit():
                results.append({"id": int(parts[-1]), "title": link.text_content() or ""})
        return results

    def upload_submission(self, zip_path: Path) -> None:
        file_input = self.page.locator('input[type="file"][name="file"]')
        file_input.set_input_files(str(zip_path))
        self.page.locator("#submit-btn").click()
        self.page.wait_for_url(f"{self.app_url}/portal/subjects/**/assignments/**")

    def get_submission_status_text(self) -> str:
        status_el = self.page.locator('[data-status], .submission-status, text=PASSED, text=FAILED, text=PENDING').first
        return (status_el.text_content() or "").strip().upper()

    def assert_submission_pending(self) -> None:
        expect(self.page.get_by_text("PENDING", exact=False).first).to_be_visible()

    def assert_submission_failed(self) -> None:
        expect(self.page.get_by_text("Failed", exact=False).first).to_be_visible()

    def assert_submission_passed(self) -> None:
        expect(self.page.get_by_text("Completed", exact=False).first).to_be_visible()

    def assert_submission_blocked(self) -> None:
        error = self.page.locator(".bg-red-50, .text-red-")
        expect(error.first).to_be_visible()

    def navigate_to_notifications(self) -> None:
        self.page.goto(f"{self.app_url}/portal/notification-preferences")

    def get_subject_id_from_url(self) -> int:
        url = self.page.url
        parts = url.split("/")
        idx = parts.index("subjects") + 1 if "subjects" in parts else -1
        return int(parts[idx]) if idx >= 0 else 0

    def get_assignment_id_from_url(self) -> int:
        url = self.page.url
        parts = url.split("/")
        idx = parts.index("assignments") + 1 if "assignments" in parts else -1
        return int(parts[idx]) if idx >= 0 else 0
