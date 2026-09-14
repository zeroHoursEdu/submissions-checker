"""Pure parsing of a subject `config.yml` into a dict.

Factored out of the plugin loader so the standalone runner and the loader share one
parsing path (no DB, no side effects).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


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
