"""Minimal Classroom + Drive REST client (httpx only)."""

from dataclasses import dataclass
from typing import Any

import httpx

from submissions_checker.core.config import Settings
from submissions_checker.services.google.matching import RosterEntry
from submissions_checker.services.google.oauth import GoogleAuthError, refresh_access_token

CLASSROOM_URL = "https://classroom.googleapis.com/v1"
DRIVE_URL = "https://www.googleapis.com/drive/v3"
MAX_FILE_BYTES = 20 * 1024 * 1024
PAGE_SIZE = 100
GOOGLE_APPS_PREFIX = "application/vnd.google-apps."
EXPORTS = {
    "application/vnd.google-apps.document": "application/pdf",
    "application/vnd.google-apps.presentation": "application/pdf",
}


class GoogleApiError(RuntimeError):
    """A Classroom/Drive API call failed (HTTP >= 400, transport error, bad body).

    `status` is the HTTP status, or 0 for a transport failure. Token/OAuth
    failures are GoogleAuthError instead.
    """

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class DriveFileRef:
    id: str
    title: str


@dataclass(frozen=True)
class StudentSubmissionRef:
    id: str
    user_id: str
    state: str
    late: bool
    files: list[DriveFileRef]


@dataclass(frozen=True)
class DownloadedFile:
    drive_id: str
    name: str
    mime: str
    modified: str
    content: bytes | None
    skipped: str | None


class ClassroomClient:
    def __init__(self, settings: Settings, refresh_token: str, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._refresh_token = refresh_token
        self._http = http
        self._access_token: str | None = None

    async def _token(self, *, force: bool = False) -> str:
        if self._access_token is None or force:
            self._access_token = await refresh_access_token(
                self._settings, self._refresh_token, self._http
            )
        return self._access_token

    async def _get(
        self, url: str, params: dict[str, Any] | None = None, *, expect_json: bool = True
    ) -> httpx.Response:
        try:
            resp = await self._send(url, params, await self._token())
            if resp.status_code == 401:
                resp = await self._send(url, params, await self._token(force=True))
        except httpx.HTTPError as exc:
            raise GoogleApiError(f"Google API transport error for {url}: {exc}", 0) from exc
        if resp.status_code == 401:
            raise GoogleAuthError(f"Google API rejected a freshly refreshed token for {url}")
        if resp.status_code >= 400:
            raise GoogleApiError(f"Google API {resp.status_code} for {url}", resp.status_code)
        if expect_json:
            try:
                resp.json()
            except ValueError as exc:
                raise GoogleApiError(
                    f"Google API returned a non-JSON body for {url}", resp.status_code
                ) from exc
        return resp

    async def _send(self, url: str, params: dict[str, Any] | None, token: str) -> httpx.Response:
        return await self._http.get(
            url, params=params, headers={"Authorization": f"Bearer {token}"}
        )

    async def _paged(self, url: str, key: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            query = {**params, "pageSize": PAGE_SIZE}
            if page_token:
                query["pageToken"] = page_token
            body = (await self._get(url, query)).json()
            items.extend(body.get(key, []))
            page_token = body.get("nextPageToken")
            if not page_token:
                return items

    async def list_courses(self) -> list[dict[str, str]]:
        rows = await self._paged(
            f"{CLASSROOM_URL}/courses", "courses", {"teacherId": "me", "courseStates": "ACTIVE"}
        )
        return [
            {"id": str(c["id"]), "name": c.get("name", ""), "section": c.get("section", "")}
            for c in rows
        ]

    async def list_students(self, course_id: str) -> list[RosterEntry]:
        rows = await self._paged(f"{CLASSROOM_URL}/courses/{course_id}/students", "students", {})
        out = []
        for s in rows:
            profile = s.get("profile", {})
            out.append(
                RosterEntry(
                    user_id=str(s.get("userId") or profile.get("id", "")),
                    full_name=profile.get("name", {}).get("fullName", ""),
                    email=profile.get("emailAddress"),
                )
            )
        return out

    async def list_coursework(self, course_id: str) -> list[dict[str, str]]:
        rows = await self._paged(
            f"{CLASSROOM_URL}/courses/{course_id}/courseWork", "courseWork", {}
        )
        return [{"id": str(w["id"]), "title": w.get("title", "")} for w in rows]

    async def list_submissions(
        self, course_id: str, coursework_id: str
    ) -> list[StudentSubmissionRef]:
        rows = await self._paged(
            f"{CLASSROOM_URL}/courses/{course_id}/courseWork/{coursework_id}/studentSubmissions",
            "studentSubmissions",
            {},
        )
        refs = []
        for r in rows:
            attachments = r.get("assignmentSubmission", {}).get("attachments", [])
            files = [
                DriveFileRef(id=str(a["driveFile"]["id"]), title=a["driveFile"].get("title", ""))
                for a in attachments
                if a.get("driveFile", {}).get("id")
            ]
            refs.append(
                StudentSubmissionRef(
                    id=str(r["id"]),
                    user_id=str(r.get("userId", "")),
                    state=r.get("state", ""),
                    late=bool(r.get("late", False)),
                    files=files,
                )
            )
        return refs

    async def file_meta(self, drive_id: str) -> dict[str, Any]:
        resp = await self._get(
            f"{DRIVE_URL}/files/{drive_id}",
            {"fields": "id,name,mimeType,size,modifiedTime", "supportsAllDrives": "true"},
        )
        meta: dict[str, Any] = resp.json()
        return meta

    async def download(self, drive_id: str) -> DownloadedFile:
        meta = await self.file_meta(drive_id)
        name = str(meta.get("name", drive_id))
        mime = str(meta.get("mimeType", ""))
        modified = str(meta.get("modifiedTime", ""))

        def skip(reason: str) -> DownloadedFile:
            return DownloadedFile(drive_id, name, mime, modified, None, reason)

        native = mime.startswith(GOOGLE_APPS_PREFIX)
        if native and mime not in EXPORTS:
            return skip("unsupported_type")
        if int(meta.get("size", 0) or 0) > MAX_FILE_BYTES:
            return skip("too_large")
        if native:
            resp = await self._get(
                f"{DRIVE_URL}/files/{drive_id}/export",
                {"mimeType": EXPORTS[mime]},
                expect_json=False,
            )
            name += ".pdf"
        else:
            resp = await self._get(
                f"{DRIVE_URL}/files/{drive_id}",
                {"alt": "media", "supportsAllDrives": "true"},
                expect_json=False,
            )
        if len(resp.content) > MAX_FILE_BYTES:
            return skip("too_large")
        return DownloadedFile(drive_id, name, mime, modified, resp.content, None)
