"""The student's code from the GitHub link in their report, for the LLM grader.

Students keep code out of the report and link a public repository instead. We find the
first github.com link in the report (DOCX text and hyperlinks, PDF text and link
annotations), download the repository tarball into memory, keep the source files and
hand them to the judge. Nothing is written to disk or stored: only a manifest of what was
sent lands in the draft. Any failure degrades to grading the report alone.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass

from submissions_checker.core.logging import get_logger

logger = get_logger(__name__)

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
    return RepoRef(owner, repo, ref or None, (path or "").strip("/") or None)


def _docx_texts(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        out = []
        if "word/_rels/document.xml.rels" in names:
            out += re.findall(
                r'Target="([^"]+)"',
                z.read("word/_rels/document.xml.rels").decode("utf-8", "replace"),
            )
        if "word/document.xml" in names:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
            # Join runs within a paragraph (Word splits a URL across runs) but keep
            # paragraphs apart so two lines never fuse into one false URL.
            for para in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S):
                out.append("".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", para)))
        return out


def _pdf_texts(data: bytes) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    out: list[str] = []
    for page in reader.pages:
        for annot in page.get("/Annots") or []:
            action = annot.get_object().get("/A") or {}
            uri = action.get("/URI") if hasattr(action, "get") else None
            if uri:
                out.append(str(uri))
        out.append(page.extract_text() or "")
    return out


def find_repo_link(files: Iterable[tuple[str, bytes]]) -> RepoRef | None:
    """The first github.com repository link in the report files, or None."""
    for name, data in files:
        lower = name.lower()
        try:
            texts = (
                _docx_texts(data)
                if lower.endswith(".docx")
                else (_pdf_texts(data) if lower.endswith(".pdf") else [])
            )
        except Exception:  # noqa: BLE001 — a broken file just has no link
            logger.info("github_link_unreadable", file=name)
            continue
        for text in texts:
            ref = parse_repo_url(text)
            if ref is not None:
                return ref
    return None
