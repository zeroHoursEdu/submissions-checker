"""Pure parsing of a subject `config.yml` into a dict.

Factored out of the plugin loader so the standalone runner and the loader share one
parsing path (no DB, no side effects).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def parse_config(raw: bytes) -> dict[str, Any]:
    """Parse raw `config.yml` bytes into a plain dict."""
    return yaml.safe_load(raw.decode("utf-8"))


def load_config(path: str | Path) -> dict[str, Any]:
    """Read and parse a `config.yml` file into a plain dict."""
    return parse_config(Path(path).read_bytes())
