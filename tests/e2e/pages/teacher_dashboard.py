"""Page object for the teacher dashboard."""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Page, expect


class TeacherDashboard:
    def __init__(self, page: Page, app_url: str) -> None:
        self.page = page
        self.app_url = app_url

    def navigate(self) -> None:
        self.page.goto(f"{self.app_url}/teacher")

    def upload_subject_config(self, zip_path: Path) -> None:
        """Upload a subject config ZIP via the hidden file input."""
        file_input = self.page.locator('input[name="config_zip"]')
        file_input.set_input_files(str(zip_path))
        # The form auto-submits via onchange handler
        self.page.wait_for_url(f"{self.app_url}/teacher**")

    def assert_subject_visible(self, subject_name: str) -> None:
        expect(self.page.get_by_text(subject_name).first).to_be_visible()

    def assert_apply_error_visible(self) -> None:
        expect(self.page.locator(".bg-red-50")).to_be_visible()

    def get_apply_result(self) -> str:
        """Return 'created', 'updated', 'unchanged', or 'error'."""
        url = self.page.url
        if "apply_result=created" in url:
            return "created"
        if "apply_result=updated" in url:
            return "updated"
        if "apply_result=unchanged" in url:
            return "unchanged"
        if "apply_error" in url:
            return "error"
        return "unknown"

    def open_subject(self, subject_name: str) -> None:
        self.page.get_by_text(subject_name).first.click()

    def click_analytics(self) -> None:
        self.page.get_by_role("link", name="Analytics").click()

    def get_subject_ids(self) -> list[int]:
        """Return list of subject IDs from subject card links."""
        links = self.page.locator('a[href^="/teacher/subjects/"]').all()
        ids = []
        for link in links:
            href = link.get_attribute("href") or ""
            parts = href.split("/")
            if len(parts) >= 4 and parts[-1].isdigit():
                ids.append(int(parts[-1]))
        return ids
