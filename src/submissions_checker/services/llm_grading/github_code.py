"""The student's code from the GitHub link in their report, for the LLM grader.

Students keep code out of the report and link a public repository instead. We find the
first github.com link in the report (DOCX text and hyperlinks, PDF text and link
annotations), download the repository tarball into memory, keep the source files and
hand them to the judge. Nothing is written to disk or stored: only a manifest of what was
sent lands in the draft. Any failure degrades to grading the report alone.
"""

from __future__ import annotations

import html
import io
import re
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

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
