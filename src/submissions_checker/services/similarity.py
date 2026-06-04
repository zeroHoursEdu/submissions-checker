"""Code similarity detection — token-level comparison between ZIP submissions."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

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


def _extract_tokens(zip_path: Path) -> list[str]:
    """Extract and tokenize all code files from a ZIP archive."""
    tokens: list[str] = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                if Path(name).suffix.lower() in _CODE_EXTENSIONS:
                    try:
                        src = zf.read(name).decode("utf-8", errors="ignore")
                        tokens.extend(_normalize(src))
                    except Exception:
                        continue
    except Exception:
        pass
    return tokens


def jaccard_similarity(a: list[str], b: list[str]) -> float:
    """Return Jaccard similarity coefficient in [0, 1]."""
    set_a, set_b = set(a), set(b)
    if not set_a and not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union else 0.0


def compare_zip_files(path_a: Path, path_b: Path) -> float:
    """Return similarity score [0, 1] between two ZIP submission archives."""
    tokens_a = _extract_tokens(path_a)
    tokens_b = _extract_tokens(path_b)
    return jaccard_similarity(tokens_a, tokens_b)
