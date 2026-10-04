import httpx
from cryptography.fernet import Fernet

from submissions_checker.core.config import Settings
from submissions_checker.services.google.client import (
    MAX_FILE_BYTES,
    ClassroomClient,
)
from submissions_checker.services.google.matching import RosterEntry

DOC = "application/vnd.google-apps.document"


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        secret_key="x" * 32,
        database_url="postgresql+asyncpg://u:p@h/db",
        google_client_id="cid",
        google_client_secret="sec",
        google_token_encryption_key=Fernet.generate_key().decode(),
    )


class Router:
    def __init__(self, routes):
        self.routes = routes  # callable(request) -> Response | None
        self.requests: list[httpx.Request] = []
        self.token_calls = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/token"):
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": f"at{self.token_calls}"})
        self.requests.append(req)
        resp = self.routes(req)
        assert resp is not None, f"unrouted {req.url}"
        return resp

    def client(self) -> tuple[ClassroomClient, httpx.AsyncClient]:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self))
        return ClassroomClient(_settings(), "rt", http), http


async def test_list_submissions_follows_pages():
    def routes(req):
        assert req.url.params["pageSize"] == "100"
        if req.url.params.get("pageToken") == "p2":
            return httpx.Response(200, json={"studentSubmissions": [{"id": "s3", "userId": "u3"}]})
        return httpx.Response(
            200,
            json={
                "nextPageToken": "p2",
                "studentSubmissions": [
                    {
                        "id": "s1",
                        "userId": "u1",
                        "state": "TURNED_IN",
                        "late": True,
                        "assignmentSubmission": {
                            "attachments": [
                                {"driveFile": {"id": "d1", "title": "a.py"}},
                                {"link": {"url": "http://x"}},
                            ]
                        },
                    },
                    {"id": "s2", "userId": "u2", "state": "NEW"},
                ],
            },
        )

    r = Router(routes)
    c, http = r.client()
    async with http:
        subs = await c.list_submissions("c", "w")
    assert [s.id for s in subs] == ["s1", "s2", "s3"]
    assert subs[0].late and [(f.id, f.title) for f in subs[0].files] == [("d1", "a.py")]
    assert subs[1].files == []


async def test_list_students_maps_roster_entries():
    def routes(req):
        return httpx.Response(
            200,
            json={
                "students": [
                    {
                        "userId": "u1",
                        "profile": {
                            "name": {"fullName": "ІП-43 Ivanov Ivan"},
                            "emailAddress": "i@x.ua",
                        },
                    },
                    {"userId": "u2", "profile": {"name": {"fullName": "No Mail"}}},
                ]
            },
        )

    c, http = Router(routes).client()
    async with http:
        got = await c.list_students("c")
    assert got == [
        RosterEntry("u1", "ІП-43 Ivanov Ivan", "i@x.ua"),
        RosterEntry("u2", "No Mail", None),
    ]


async def test_list_courses_and_coursework_map_fields():
    def routes(req):
        if req.url.path.endswith("/courses"):
            assert (
                req.url.params["teacherId"] == "me" and req.url.params["courseStates"] == "ACTIVE"
            )
            return httpx.Response(200, json={"courses": [{"id": 1, "name": "N", "section": "S"}]})
        return httpx.Response(200, json={"courseWork": [{"id": "w", "title": "T"}]})

    c, http = Router(routes).client()
    async with http:
        assert await c.list_courses() == [{"id": "1", "name": "N", "section": "S"}]
        assert await c.list_coursework("1") == [{"id": "w", "title": "T"}]


def _drive(meta, export=b"PDF", media=b"RAW"):
    def routes(req):
        if req.url.path.endswith("/export"):
            assert req.url.params["mimeType"] == "application/pdf"
            return httpx.Response(200, content=export)
        if req.url.params.get("alt") == "media":
            return httpx.Response(200, content=media)
        return httpx.Response(200, json=meta)

    return routes


async def test_download_exports_google_doc_to_pdf():
    r = Router(_drive({"id": "d", "name": "Report", "mimeType": DOC, "modifiedTime": "T"}))
    c, http = r.client()
    async with http:
        f = await c.download("d")
    assert f.name == "Report.pdf" and f.content == b"PDF" and f.skipped is None
    assert r.requests[-1].url.path.endswith("/export")


async def test_download_plain_file_uses_media():
    r = Router(_drive({"id": "d", "name": "a.py", "mimeType": "text/x-python", "size": "3"}))
    c, http = r.client()
    async with http:
        f = await c.download("d")
    assert f.name == "a.py" and f.content == b"RAW"


async def test_download_skips_too_large():
    r = Router(
        _drive(
            {
                "id": "d",
                "name": "big.zip",
                "mimeType": "application/zip",
                "size": str(MAX_FILE_BYTES + 1),
            }
        )
    )
    c, http = r.client()
    async with http:
        f = await c.download("d")
    assert f.content is None and f.skipped == "too_large"
    assert len(r.requests) == 1  # metadata only


async def test_download_skips_unsupported_native():
    r = Router(
        _drive({"id": "d", "name": "S", "mimeType": "application/vnd.google-apps.spreadsheet"})
    )
    c, http = r.client()
    async with http:
        f = await c.download("d")
    assert f.content is None and f.skipped == "unsupported_type"
    assert len(r.requests) == 1


async def test_retries_once_on_401():
    state = {"n": 0}

    def routes(req):
        state["n"] += 1
        if state["n"] == 1:
            assert req.headers["authorization"] == "Bearer at1"
            return httpx.Response(401, json={})
        assert req.headers["authorization"] == "Bearer at2"
        return httpx.Response(200, json={"courses": []})

    r = Router(routes)
    c, http = r.client()
    async with http:
        assert await c.list_courses() == []
    assert r.token_calls == 2
