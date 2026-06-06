"""Page object for the teacher subject detail page."""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Page, expect


class SubjectPage:
    def __init__(self, page: Page, app_url: str) -> None:
        self.page = page
        self.app_url = app_url

    def navigate(self, subject_id: int) -> None:
        self.page.goto(f"{self.app_url}/teacher/subjects/{subject_id}")

    def get_enrolled_count(self) -> int:
        count_text = self.page.locator(".lg\\:col-span-2 .text-xs.text-slate-400").text_content()
        try:
            return int((count_text or "0").strip())
        except ValueError:
            return 0

    def import_students_via_api(self, subject_id: int, csv_path: Path) -> dict:
        """Submit CSV to the subject import endpoint via Playwright request API.

        Returns the URL params after redirect (imported, skipped counts).
        """
        with open(csv_path, "rb") as f:
            csv_bytes = f.read()
        response = self.page.request.post(
            f"{self.app_url}/teacher/subjects/{subject_id}/students/import",
            multipart={"file": {"name": "students.csv", "mimeType": "text/csv", "buffer": csv_bytes}},
        )
        return {"status": response.status, "url": response.url}

    def get_assignment_links(self) -> list[dict]:
        """Return list of {id, title} for all assignments."""
        links = self.page.locator('a[href*="/assignments/"]').all()
        results = []
        for link in links:
            href = link.get_attribute("href") or ""
            parts = href.split("/")
            if len(parts) >= 6 and parts[-1].isdigit():
                results.append({"id": int(parts[-1]), "title": link.text_content() or ""})
        return results

    def open_assignment(self, assignment_id: int) -> None:
        self.page.goto(f"{self.app_url}/teacher/subjects/{self.current_subject_id}/assignments/{assignment_id}")

    def get_student_count_from_page(self) -> int:
        el = self.page.locator('.lg\\:col-span-2 span.text-xs').first
        try:
            return int(el.text_content() or "0")
        except ValueError:
            return 0

    @property
    def current_subject_id(self) -> int:
        url = self.page.url
        parts = url.split("/")
        idx = parts.index("subjects") + 1 if "subjects" in parts else -1
        return int(parts[idx]) if idx >= 0 else 0

    def assert_student_in_list(self, full_name: str) -> None:
        expect(self.page.get_by_text(full_name)).to_be_visible()

    def click_view_feedback(self) -> None:
        self.page.get_by_role("link", name="View Feedback").click()

    def request_feedback(self) -> None:
        btn = self.page.get_by_role("button", name="Request Feedback")
        btn.click()
        self.page.wait_for_url(f"{self.app_url}/teacher/subjects/**")

    def assert_feedback_sent(self) -> None:
        expect(self.page.locator(".bg-green-50")).to_be_visible()
