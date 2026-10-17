"""fetch_code: tarball in memory, filtered, capped, failure-tolerant."""

from __future__ import annotations

import io
import tarfile
import tempfile
from pathlib import Path

import httpx
import pytest

from submissions_checker.services.llm_grading import github_code as gh


def _tar(
    entries: dict[str, bytes], *, top: str = "a-lab-abc1234", link: str | None = None
) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in entries.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        if link:
            info = tarfile.TarInfo(f"{top}/evil")
            info.type = tarfile.SYMTYPE
            info.linkname = link
            t.addfile(info)
    return buf.getvalue()


def test_select_files_filters_and_reports_commit() -> None:
    tar = _tar(
        {
            "lab4/main.cpp": b"int main(){}",
            "lab4/x64/Debug/a.obj": b"x",
            "lab4/CMakeLists.txt": b"c",
            "README.md": b"r",
            ".git/config": b"g",
            "lab4/App.vcxproj": b"v",
        },
        link="/etc/passwd",
    )
    commit, files, _ = gh.select_files(tar, None)
    assert commit == "abc1234"
    assert sorted(f.path for f in files) == [
        "lab4/App.vcxproj",
        "lab4/CMakeLists.txt",
        "lab4/main.cpp",
    ]
    assert "evil" not in [f.path for f in files]


def test_links_and_devices_skipped() -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, typ in (
            ("link.cpp", tarfile.SYMTYPE),
            ("hard.cpp", tarfile.LNKTYPE),
            ("dev.cpp", tarfile.CHRTYPE),
            ("fifo.cpp", tarfile.FIFOTYPE),
        ):
            info = tarfile.TarInfo(f"a-b-c/{name}")
            info.type = typ
            info.linkname = "/etc/passwd"
            t.addfile(info)
        info = tarfile.TarInfo("a-b-c/ok.cpp")
        info.size = 1
        t.addfile(info, io.BytesIO(b"x"))
    _, files, _ = gh.select_files(buf.getvalue(), None)
    assert [f.path for f in files] == ["ok.cpp"]


def test_select_files_under_path() -> None:
    tar = _tar({"lab4/main.cpp": b"a", "lab3/main.cpp": b"b"})
    _, files, _ = gh.select_files(tar, "lab4")
    assert [f.path for f in files] == ["lab4/main.cpp"]


def test_caps_skip_the_rest() -> None:
    tar = _tar({f"f{i:03}.cpp": b"x" * 10 for i in range(gh.MAX_FILES + 5)})
    _, files, skipped = gh.select_files(tar, None)
    assert len(files) == gh.MAX_FILES and len(skipped) == 5
    tar = _tar({"big.cpp": b"x" * (gh.MAX_TOTAL + 1), "small.cpp": b"y"})
    _, files, skipped = gh.select_files(tar, None)
    assert [f.path for f in files] == ["small.cpp"] and skipped == ["big.cpp"]


def test_unsafe_paths_dropped() -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name in ("a-b-c/../../x.cpp", "/abs.cpp"):
            info = tarfile.TarInfo(name)
            info.size = 1
            t.addfile(info, io.BytesIO(b"x"))
    _, files, _ = gh.select_files(buf.getvalue(), None)
    assert files == []


def test_too_many_members_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gh, "MAX_MEMBERS", 3)
    tar = _tar({f"d{i}.bin": b"x" for i in range(10)})
    with pytest.raises(gh.RepoTooLarge):
        gh.select_files(tar, None)


def test_too_much_scanned_data_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gh, "MAX_SCANNED", 50)
    tar = _tar({"a.bin": b"x" * 30, "b.bin": b"x" * 30, "c.cpp": b"y"})
    with pytest.raises(gh.RepoTooLarge):
        gh.select_files(tar, None)


def _client(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_fetch_ok_and_url() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=_tar({"main.cpp": b"int"}))

    ref = gh.RepoRef("a", "lab", "main", None)
    async with _client(handler) as http:
        bundle = await gh.fetch_code(http, ref)
    assert seen == ["https://api.github.com/repos/a/lab/tarball/main"]
    assert bundle.error is None and [f.path for f in bundle.files] == ["main.cpp"]
    assert bundle.commit == "abc1234"
    assert bundle.manifest() == {
        "url": "https://github.com/a/lab/tree/main",
        "commit": "abc1234",
        "files": [{"path": "main.cpp", "size": 3}],
        "skipped": [],
        "error": None,
    }


@pytest.mark.asyncio
async def test_fetch_follows_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(302, headers={"location": "https://codeload.github.com/x"})
        return httpx.Response(200, content=_tar({"main.cpp": b"int"}))

    async with _client(handler) as http:
        bundle = await gh.fetch_code(http, gh.RepoRef("a", "b"))
    assert bundle.error is None and len(bundle.files) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error"), [(404, "not_found"), (403, "forbidden_or_rate_limited"), (500, "http_500")]
)
async def test_fetch_http_errors_degrade(status: int, error: str) -> None:
    async with _client(lambda r: httpx.Response(status)) as http:
        bundle = await gh.fetch_code(http, gh.RepoRef("a", "b"))
    assert bundle.files == () and bundle.error == error


@pytest.mark.asyncio
async def test_fetch_oversized_aborts() -> None:
    big = b"x" * (gh.MAX_DOWNLOAD + 1)
    async with _client(lambda r: httpx.Response(200, content=big)) as http:
        bundle = await gh.fetch_code(http, gh.RepoRef("a", "b"))
    assert bundle.error == "too_large" and bundle.files == ()


@pytest.mark.asyncio
async def test_fetch_hostile_tarball_reports_too_large_and_drops_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gh, "MAX_MEMBERS", 3)
    tar = _tar({"main.cpp": b"1", **{f"d{i}.bin": b"x" for i in range(10)}})
    async with _client(lambda r: httpx.Response(200, content=tar)) as http:
        bundle = await gh.fetch_code(http, gh.RepoRef("a", "b"))
    assert bundle.error == "too_large" and bundle.files == ()


@pytest.mark.asyncio
async def test_fetch_scanned_bytes_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gh, "MAX_SCANNED", 10)
    tar = _tar({"main.cpp": b"1", "blob.bin": b"x" * 100})
    async with _client(lambda r: httpx.Response(200, content=tar)) as http:
        bundle = await gh.fetch_code(http, gh.RepoRef("a", "b"))
    assert bundle.error == "too_large" and bundle.files == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", [b"not a tarball at all", b"\x1f\x8b\x08\x00garbage", _tar({"a.cpp": b"x"})[:40], b""]
)
async def test_fetch_garbage_body_is_error_bundle(body: bytes) -> None:
    async with _client(lambda r: httpx.Response(200, content=body)) as http:
        bundle = await gh.fetch_code(http, gh.RepoRef("a", "b"))
    assert bundle.files == () and bundle.error is not None


@pytest.mark.asyncio
async def test_fetch_network_error_degrades() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async with _client(handler) as http:
        bundle = await gh.fetch_code(http, gh.RepoRef("a", "b"))
    assert bundle.files == () and bundle.error == "ConnectError"


@pytest.mark.asyncio
async def test_nothing_written_to_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    def boom(*a: object, **k: object) -> None:
        raise AssertionError("tempfile used")

    for name in (
        "mkstemp",
        "mkdtemp",
        "TemporaryFile",
        "NamedTemporaryFile",
        "SpooledTemporaryFile",
        "TemporaryDirectory",
    ):
        monkeypatch.setattr(tempfile, name, boom)
    monkeypatch.setattr(tarfile.TarFile, "extract", boom)
    monkeypatch.setattr(tarfile.TarFile, "extractall", boom)
    async with _client(lambda r: httpx.Response(200, content=_tar({"m.cpp": b"1"}))) as http:
        bundle = await gh.fetch_code(http, gh.RepoRef("a", "b"))
    assert bundle.error is None and len(bundle.files) == 1
    assert list(tmp_path.iterdir()) == []
