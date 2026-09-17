"""Pure parsing of a subject `config.yml` into a dict.

Factored out of the plugin loader so the standalone runner and the loader share one
parsing path (no DB, no side effects).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

# A subject code is used verbatim as a directory name under the plugins root (and as an
# S3 key prefix). One path component, ASCII, no leading dot, no "..": anything else could
# escape the plugins directory when the tree is extracted and swapped into place.
_SUBJECT_CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def validate_subject_code(code: object) -> str:
    """Return ``code`` if it is safe to use as a single path component, else ValueError."""
    if not isinstance(code, str) or not _SUBJECT_CODE_RE.fullmatch(code) or ".." in code:
        raise ValueError(
            "subjectCode must be 1-64 characters of letters, digits, '_', '-' or '.', "
            "start with a letter or digit, and not contain '..'"
        )
    return code


def parse_config(raw: bytes) -> Any:
    """Parse raw `config.yml` bytes into whatever YAML document it holds.

    The return type is deliberately `Any`, not `dict`: an empty file parses to
    `None`, and the runner reuses this to read a test suite whose top level it
    validates itself. Callers check the shape and report it in their own terms.
    """
    return yaml.safe_load(raw.decode("utf-8"))


def load_config(path: str | Path) -> Any:
    """Read and parse a `config.yml` file. See `parse_config` for the return type."""
    return parse_config(Path(path).read_bytes())
