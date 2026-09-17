"""Unit tests for services.similarity — Jaccard scoring + ZIP comparison.

ZIPs are built in-memory and written to tmp_path so compare_zip_files exercises
the real extraction/tokenization path without external fixtures.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from submissions_checker.services.similarity import (
    _normalize,
    compare_zip_files,
    jaccard_similarity,
)

# ── jaccard_similarity ────────────────────────────────────────────────────────


def test_identical_token_lists_score_one() -> None:
    toks = ["a", "b", "c"]
    assert jaccard_similarity(toks, list(toks)) == 1.0


def test_disjoint_token_lists_score_zero() -> None:
    assert jaccard_similarity(["a", "b"], ["c", "d"]) == 0.0


def test_partial_overlap() -> None:
    # {a,b,c} vs {b,c,d}: intersection 2, union 4 -> 0.5
    assert jaccard_similarity(["a", "b", "c"], ["b", "c", "d"]) == 0.5


def test_duplicates_collapse_to_sets() -> None:
    # multiplicity is ignored (set semantics)
    assert jaccard_similarity(["a", "a", "a", "b"], ["a", "b"]) == 1.0


def test_both_empty_returns_zero() -> None:
    assert jaccard_similarity([], []) == 0.0


def test_one_empty_returns_zero() -> None:
    assert jaccard_similarity(["a"], []) == 0.0
    assert jaccard_similarity([], ["a"]) == 0.0


def test_score_bounded_unit_interval() -> None:
    s = jaccard_similarity(["a", "b", "c"], ["a", "x"])
    assert 0.0 <= s <= 1.0


# ── _normalize ────────────────────────────────────────────────────────────────


def test_normalize_lowercases_identifiers() -> None:
    assert _normalize("Foo BAR baz") == ["foo", "bar", "baz"]


def test_normalize_strips_line_comments() -> None:
    assert _normalize("code # secret\nmore // gone") == ["code", "more"]


def test_normalize_strips_string_literals() -> None:
    toks = _normalize('x = "hidden text here"')
    assert "hidden" not in toks
    assert "x" in toks


def test_normalize_strips_block_comments() -> None:
    src = "a /* block\ncomment b */ c"
    toks = _normalize(src)
    assert toks == ["a", "c"]


def test_normalize_strips_triple_quoted_docstrings() -> None:
    src = 'foo\n"""this is a docstring should vanish"""\nbar'
    toks = _normalize(src)
    assert "docstring" not in toks
    assert toks == ["foo", "bar"]


# ── compare_zip_files ─────────────────────────────────────────────────────────


def _make_zip(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as zf:
        for fn, content in files.items():
            zf.writestr(fn, content)
    return p


def test_identical_zips_score_one(tmp_path: Path) -> None:
    src = "def add(a, b):\n    return a + b\n"
    a = _make_zip(tmp_path, "a.zip", {"main.py": src})
    b = _make_zip(tmp_path, "b.zip", {"main.py": src})
    assert compare_zip_files(a, b) == 1.0


def test_completely_different_zips_score_low(tmp_path: Path) -> None:
    a = _make_zip(tmp_path, "a.zip", {"main.py": "def alpha(): return one\n"})
    b = _make_zip(tmp_path, "b.zip", {"main.py": "class Beta: pass\n"})
    score = compare_zip_files(a, b)
    assert score < 0.3


def test_non_code_files_are_ignored(tmp_path: Path) -> None:
    # README differs wildly but isn't a code extension -> still identical code.
    src = "x = compute(value)\n"
    a = _make_zip(tmp_path, "a.zip", {"m.py": src, "README.md": "totally different prose alpha"})
    b = _make_zip(tmp_path, "b.zip", {"m.py": src, "README.md": "beta gamma delta words"})
    assert compare_zip_files(a, b) == 1.0


def test_comments_do_not_affect_similarity(tmp_path: Path) -> None:
    a = _make_zip(tmp_path, "a.zip", {"m.py": "result = transform(data)  # mine\n"})
    b = _make_zip(
        tmp_path, "b.zip", {"m.py": "result = transform(data)  # entirely different note\n"}
    )
    assert compare_zip_files(a, b) == 1.0


def test_missing_zip_yields_zero(tmp_path: Path) -> None:
    a = _make_zip(tmp_path, "a.zip", {"m.py": "x = y\n"})
    missing = tmp_path / "nope.zip"
    # _extract_tokens swallows errors -> empty token list -> jaccard 0
    assert compare_zip_files(a, missing) == 0.0


# ── pairwise report ───────────────────────────────────────────────────────────


def test_pairwise_returns_sorted_pairs() -> None:
    from submissions_checker.services.similarity import pairwise_similarity

    items = {1: frozenset({"a", "b", "c"}), 2: frozenset({"a", "b", "d"}), 3: frozenset({"x"})}
    pairs = pairwise_similarity(items)
    assert len(pairs) == 3
    assert pairs[0][:2] == (1, 2) and abs(pairs[0][2] - 0.5) < 1e-9
    assert all(0.0 <= s <= 1.0 for _, _, s in pairs)
    assert [s for _, _, s in pairs] == sorted((s for _, _, s in pairs), reverse=True)


def test_token_set_for_zip_reads_code_files(tmp_path: Path) -> None:
    from submissions_checker.services.similarity import token_set_for_zip

    p = tmp_path / "a.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("main.py", "def add(a, b): return a + b")
        zf.writestr("notes.txt", "ignored words")
    tokens = token_set_for_zip(p)
    assert {"def", "add", "return"} <= tokens
    assert "ignored" not in tokens
