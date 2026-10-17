"""The student's code from the GitHub link in their report, for the LLM grader.

Students keep code out of the report and link a public repository instead. We find the
first github.com link in the report (DOCX text and hyperlinks, PDF text and link
annotations), download the repository tarball into memory, keep the source files and
hand them to the judge. Nothing is written to disk or stored: only a manifest of what was
sent lands in the draft. Any failure degrades to grading the report alone.
"""

from __future__ import annotations

import asyncio
import gzip
import html
import io
import posixpath
import re
import tarfile
import zipfile
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

import httpx

from submissions_checker.core.logging import get_logger

logger = get_logger(__name__)

MAX_DOCX_PART = 20 * 1024 * 1024  # decompressed bytes read from one DOCX part
MAX_PDF_PAGES = 200
MAX_REPO_LEN = 100
MAX_REF_LEN = 500

# Attributes may not contain "<" or ">", so a scan never runs past the next tag
# (an unterminated "<w:t " cannot make this quadratic) and <w:tab/> is not a text run.
_RUN_RE = re.compile(r"<w:t(?:\s[^<>]*)?>([^<]*)</w:t>")

_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]+?)"
    r"(?:\.git)?(?:/tree/([^/\s)\]\"'<>,;]+)(?:/([^\s)\]\"'<>,;?#]+?))?)?/?(?=[\s)\]\"'<>,;.?#]|$)"
)


@dataclass(frozen=True)
class RepoRef:
    owner: str
    repo: str
    ref: str | None = None
    path: str | None = None

    @property
    def url(self) -> str:
        base = f"https://github.com/{self.owner}/{self.repo}"
        if self.ref:
            base += f"/tree/{self.ref}" + (f"/{self.path}" if self.path else "")
        return base


def parse_repo_url(text: str) -> RepoRef | None:
    m = _URL_RE.search(text)
    if m is None:
        return None
    owner, repo, ref, path = m.groups()
    path = (path or "").strip("/") or None
    segments = [repo, *(ref or "").split("/"), *(path or "").split("/")]
    if any(seg in (".", "..") for seg in segments):
        return None
    if len(repo) > MAX_REPO_LEN or len(ref or "") > MAX_REF_LEN or len(path or "") > MAX_REF_LEN:
        return None
    return RepoRef(owner, repo, ref or None, path)


def _read_part(z: zipfile.ZipFile, name: str) -> str:
    """One DOCX part as text; a part that inflates past the cap makes the file unreadable."""
    if z.getinfo(name).file_size > MAX_DOCX_PART:
        raise ValueError(f"{name} exceeds {MAX_DOCX_PART} bytes")
    with z.open(name) as f:
        raw = f.read(MAX_DOCX_PART + 1)  # the header size can lie; bound the real read too
    if len(raw) > MAX_DOCX_PART:
        raise ValueError(f"{name} exceeds {MAX_DOCX_PART} bytes")
    return raw.decode("utf-8", "replace")


def _docx_texts(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        out: list[str] = []
        if "word/_rels/document.xml.rels" in names:
            rels = _read_part(z, "word/_rels/document.xml.rels")
            out += [html.unescape(t) for t in re.findall(r'Target="([^"]+)"', rels)]
        if "word/document.xml" in names:
            xml = _read_part(z, "word/document.xml")
            # Join runs within a paragraph (Word splits a URL across runs) but keep
            # paragraphs apart so two lines never fuse into one false URL. Splitting
            # on the closing tag is linear, unlike matching <w:p>..</w:p> lazily.
            for para in xml.split("</w:p>"):
                out.append(html.unescape("".join(_RUN_RE.findall(para))))
        return out


def _pdf_texts(data: bytes) -> Iterator[str]:
    """Link URIs of the first pages, then page text one page at a time (lazy)."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = list(reader.pages[:MAX_PDF_PAGES])
    for page in pages:
        try:
            annots = list(page.get("/Annots") or [])
        except Exception:  # noqa: BLE001 — a weird page has no annotations
            continue
        for annot in annots:
            try:
                action = annot.get_object().get("/A") or {}
                uri = action.get("/URI")
            except Exception:  # noqa: BLE001 — skip one odd annotation, keep the rest
                continue
            if uri:
                yield str(uri)
    for page in pages:
        try:
            yield page.extract_text() or ""
        except Exception:  # noqa: BLE001
            continue


def find_repo_link(files: Iterable[tuple[str, bytes]]) -> RepoRef | None:
    """The first github.com repository link in the report files, or None."""
    for name, data in files:
        lower = name.lower()
        texts: Iterable[str] = ()
        try:
            if lower.endswith(".docx"):
                texts = _docx_texts(data)
            elif lower.endswith(".pdf"):
                texts = _pdf_texts(data)
            # _pdf_texts is lazy, so iterate inside the try: a broken file just has no link.
            for text in texts:
                ref = parse_repo_url(text)
                if ref is not None:
                    return ref
        except Exception:  # noqa: BLE001
            logger.info("github_link_unreadable", file=name)
    return None


MAX_DOWNLOAD = 20 * 1024 * 1024
MAX_FILES = 200
MAX_TOTAL = 1024 * 1024
MAX_MEMBERS = 5000  # tar entries looked at before the archive is called hostile
MAX_SCANNED = 200 * 1024 * 1024  # sum of regular-file sizes looked at (bounds gzip bombs)
TIMEOUT = 20.0
KEEP_EXT = (".cpp", ".cc", ".c", ".h", ".hpp", ".rc", ".txt")
KEEP_NAMES = ("CMakeLists.txt",)
KEEP_SUFFIX = (".sln", ".vcxproj")
SKIP_DIRS = frozenset({".git", "x64", "Debug", "Release", ".vs", "build"})


class RepoTooLarge(Exception):
    """The archive has more entries or data than we are willing to walk."""


class _BoundedReader:
    """Decompressed byte stream that refuses to yield more than MAX_SCANNED bytes.

    tarfile swallows non-file payloads (PAX/long-name headers, unsupported types)
    without telling us, so the bound has to sit under it, on the stream itself.
    """

    def __init__(self, raw: io.BufferedIOBase) -> None:
        self._raw = raw
        self._count = 0

    def read(self, size: int = -1) -> bytes:
        data = self._raw.read(size)
        self._count += len(data)
        if self._count > MAX_SCANNED:
            raise RepoTooLarge("too much data")
        return data


def _open_stream(tar_bytes: bytes) -> _BoundedReader:
    raw: io.BufferedIOBase = io.BytesIO(tar_bytes)
    if tar_bytes[:2] == b"\x1f\x8b":
        raw = gzip.GzipFile(fileobj=raw)
    return _BoundedReader(raw)


@dataclass(frozen=True)
class CodeFile:
    path: str
    content: bytes


@dataclass(frozen=True)
class CodeBundle:
    ref: RepoRef | None
    commit: str | None = None
    files: tuple[CodeFile, ...] = ()
    skipped: tuple[str, ...] = ()
    error: str | None = None

    def manifest(self) -> dict[str, object]:
        return {
            "url": self.ref.url if self.ref else None,
            "commit": self.commit,
            "files": [{"path": f.path, "size": len(f.content)} for f in self.files],
            "skipped": list(self.skipped),
            "error": self.error,
        }


def _keep(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return False
    name = parts[-1]
    return name in KEEP_NAMES or name.endswith(KEEP_EXT) or name.endswith(KEEP_SUFFIX)


def select_files(
    tar_bytes: bytes, path: str | None
) -> tuple[str | None, list[CodeFile], list[str]]:
    """Source files from a GitHub tarball, in memory; unsafe or filtered entries dropped.

    Raises RepoTooLarge past MAX_MEMBERS entries or MAX_SCANNED bytes of member data.
    Only extractfile() reads are used: nothing is extracted to disk.
    """
    files: list[CodeFile] = []
    skipped: list[str] = []
    commit: str | None = None
    total = 0
    members = 0
    scanned = 0
    prefix = (path.strip("/") + "/") if path else ""
    # Stream mode: one forward pass over the bounded decompressed bytes.
    with tarfile.open(fileobj=_open_stream(tar_bytes), mode="r|") as tar:  # type: ignore[call-overload]
        for member in tar:
            members += 1
            if members > MAX_MEMBERS:
                raise RepoTooLarge("too many entries")
            name = member.name
            if name.startswith("/") or ".." in name.split("/") or not member.isfile():
                continue
            scanned += member.size
            if scanned > MAX_SCANNED:
                raise RepoTooLarge("too much data")
            top, _, rel = name.partition("/")
            if commit is None and "-" in top:
                commit = top.rsplit("-", 1)[1]
            rel = posixpath.normpath(rel) if rel else ""
            if not rel or rel.startswith("..") or not rel.startswith(prefix) or not _keep(rel):
                continue
            if len(files) >= MAX_FILES or total + member.size > MAX_TOTAL:
                skipped.append(rel)
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            data = handle.read()
            files.append(CodeFile(rel, data))
            total += len(data)
    return commit, files, skipped


async def fetch_code(http: httpx.AsyncClient, ref: RepoRef) -> CodeBundle:
    """The repository's source files, or a bundle naming why there are none. Never raises."""
    url = f"https://api.github.com/repos/{ref.owner}/{ref.repo}/tarball" + (
        f"/{ref.ref}" if ref.ref else ""
    )
    try:
        async with asyncio.timeout(TIMEOUT):  # one deadline for the whole download
            async with http.stream(
                "GET",
                url,
                follow_redirects=True,
                timeout=TIMEOUT,
                headers={"Accept": "application/vnd.github+json"},
            ) as resp:
                host = resp.url.host
                if host != "github.com" and not host.endswith(".github.com"):
                    return CodeBundle(ref, error="bad_redirect")
                if resp.status_code == 404:
                    return CodeBundle(ref, error="not_found")
                if resp.status_code in (401, 403, 429):
                    return CodeBundle(ref, error="forbidden_or_rate_limited")
                if resp.status_code != 200:
                    return CodeBundle(ref, error=f"http_{resp.status_code}")
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf += chunk
                    if len(buf) > MAX_DOWNLOAD:
                        return CodeBundle(ref, error="too_large")
        body = bytes(buf)
        del buf
        commit, files, skipped = await asyncio.to_thread(select_files, body, ref.path)
    except (RepoTooLarge, MemoryError):
        return CodeBundle(ref, error="too_large")
    except TimeoutError:
        return CodeBundle(ref, error="timeout")
    except (httpx.HTTPError, tarfile.TarError, EOFError, zlib.error, OSError, ValueError) as exc:
        logger.info("github_fetch_failed", url=ref.url, error=type(exc).__name__)
        return CodeBundle(ref, error=type(exc).__name__)
    return CodeBundle(
        ref, commit, tuple(files), tuple(skipped), None if files else "no_source_files"
    )
