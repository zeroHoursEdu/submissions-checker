"""Code similarity detection — token-level comparison between ZIP submissions."""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

from submissions_checker.core.logging import get_logger

logger = get_logger(__name__)

ZipSource = Path | bytes

_CODE_EXTENSIONS = {".py", ".java", ".c", ".cpp", ".h", ".js", ".ts", ".cs", ".go", ".rs"}
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")


def _normalize(source: str) -> list[str]:
    """Strip comments/strings, lowercase, return sorted unique tokens."""
    # Remove block strings/comments (Python, C-style)
    source = re.sub(r'""".*?"""', " ", source, flags=re.DOTALL)
    source = re.sub(r"'''.*?'''", " ", source, flags=re.DOTALL)
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    # Remove line comments
    source = re.sub(r"(#|//).*", " ", source)
    # Remove string literals
    source = re.sub(r'"[^"]*"', " ", source)
    source = re.sub(r"'[^']*'", " ", source)
    return [t.lower() for t in _IDENTIFIER.findall(source)]


def _extract_tokens(src: ZipSource) -> list[str]:
    """Extract and tokenize all code files from a ZIP archive (path or bytes)."""
    tokens: list[str] = []
    try:
        zf_src = io.BytesIO(src) if isinstance(src, bytes) else src
        with zipfile.ZipFile(zf_src) as zf:
            for name in zf.namelist():
                if Path(name).suffix.lower() in _CODE_EXTENSIONS:
                    try:
                        file_src = zf.read(name).decode("utf-8", errors="ignore")
                        tokens.extend(_normalize(file_src))
                    except Exception:
                        continue
    except Exception as exc:  # a broken archive must not break the upload that triggered it
        logger.warning("similarity_tokenize_failed", error=str(exc))
    return tokens


def jaccard_similarity(a: list[str], b: list[str]) -> float:
    """Return Jaccard similarity coefficient in [0, 1]."""
    set_a, set_b = set(a), set(b)
    if not set_a and not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union else 0.0


def compare_zip_files(path_a: ZipSource, path_b: ZipSource) -> float:
    """Return similarity score [0, 1] between two ZIP submission archives (paths or bytes)."""
    tokens_a = _extract_tokens(path_a)
    tokens_b = _extract_tokens(path_b)
    return jaccard_similarity(tokens_a, tokens_b)


def token_set_for_zip(zip_src: ZipSource) -> frozenset[str]:
    """The distinct identifier tokens of every code file in a ZIP archive (path or bytes)."""
    return frozenset(_extract_tokens(zip_src))


def pairwise_similarity(items: dict[int, frozenset[str]]) -> list[tuple[int, int, float]]:
    """Jaccard score for every unordered pair of *items*, highest first.

    Quadratic in the number of items; callers cap the input (a few hundred
    submissions compare in well under a second).
    """
    keys = sorted(items)
    out: list[tuple[int, int, float]] = []
    for i, a in enumerate(keys):
        set_a = items[a]
        for b in keys[i + 1 :]:
            set_b = items[b]
            union = len(set_a | set_b)
            score = len(set_a & set_b) / union if union else 0.0
            out.append((a, b, score))
    out.sort(key=lambda t: t[2], reverse=True)
    return out
