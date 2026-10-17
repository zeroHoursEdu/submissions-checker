"""Find the student's GitHub repository link in a DOCX or PDF report."""

from __future__ import annotations

import io
import zipfile

import pytest
from pypdf import PdfWriter
from pypdf.annotations import Link

from submissions_checker.services.llm_grading import github_code as gh


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("див. https://github.com/ivan-p/lab4", ("ivan-p", "lab4", None, None)),
        ("(https://github.com/ivan/dist.git).", ("ivan", "dist", None, None)),
        ("https://github.com/ivan/dist/tree/main/lab4).", ("ivan", "dist", "main", "lab4")),
        ("(https://github.com/ivan/dist/tree/main/sub/dir).", ("ivan", "dist", "main", "sub/dir")),
        ("https://github.com/ivan/dist/tree/dev", ("ivan", "dist", "dev", None)),
        ("https://github.com/ivan/dist.", ("ivan", "dist", None, None)),
        ("http://www.github.com/a/b/", ("a", "b", None, None)),
        ("https://gist.github.com/a/b", None),
        ("no link here", None),
    ],
)
def test_parse_repo_url(text: str, expected: tuple[str, ...] | None) -> None:
    ref = gh.parse_repo_url(text)
    assert (None if ref is None else (ref.owner, ref.repo, ref.ref, ref.path)) == expected


def test_url_property_roundtrip() -> None:
    assert gh.RepoRef("a", "b").url == "https://github.com/a/b"
    assert gh.RepoRef("a", "b", "main", "x/y").url == "https://github.com/a/b/tree/main/x/y"


def _docx(body_xml: str, rel_target: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "word/document.xml",
            f'<w:document xmlns:w="w"><w:body><w:p>{body_xml}</w:p></w:body></w:document>',
        )
        rels = (
            ""
            if rel_target is None
            else f'<Relationship Id="rId9" Type="hyperlink" Target="{rel_target}" TargetMode="External"/>'
        )
        z.writestr("word/_rels/document.xml.rels", f"<Relationships>{rels}</Relationships>")
    return buf.getvalue()


def _run(text: str) -> str:
    return f"<w:r><w:t>{text}</w:t></w:r>"


def test_docx_text_link() -> None:
    ref = gh.find_repo_link([("Звіт.docx", _docx(_run("Код: https://github.com/a/lab1")))])
    assert ref is not None
    assert (ref.owner, ref.repo) == ("a", "lab1")


def test_docx_url_split_across_runs() -> None:
    body = _run("https://github.com/") + _run("a/") + _run("split).")
    ref = gh.find_repo_link([("r.docx", _docx(body))])
    assert ref is not None
    assert (ref.owner, ref.repo) == ("a", "split")


def test_docx_hyperlink_with_hidden_url() -> None:
    ref = gh.find_repo_link(
        [("r.docx", _docx(_run("мій репозиторій"), "https://github.com/a/lab2"))]
    )
    assert ref is not None
    assert ref.repo == "lab2"


def _pdf_with_link(uri: str) -> bytes:
    w = PdfWriter()
    w.add_blank_page(200, 200)
    w.add_annotation(page_number=0, annotation=Link(rect=(10, 10, 100, 30), url=uri))
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_pdf_annotation_link() -> None:
    ref = gh.find_repo_link([("r.pdf", _pdf_with_link("https://github.com/b/lab3/tree/main/src"))])
    assert ref is not None
    assert (ref.repo, ref.ref, ref.path) == ("lab3", "main", "src")


def test_broken_files_are_skipped() -> None:
    assert gh.find_repo_link([("r.pdf", b"not a pdf"), ("x.docx", b"nope")]) is None


def test_first_file_with_a_link_wins() -> None:
    files = [
        ("a.docx", _docx(_run("nothing"))),
        ("b.docx", _docx(_run("https://github.com/x/first"))),
        ("c.docx", _docx(_run("https://github.com/x/second"))),
    ]
    ref = gh.find_repo_link(files)
    assert ref is not None
    assert ref.repo == "first"
