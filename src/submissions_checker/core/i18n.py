"""Vocabulary loader and per-request language resolver."""

from pathlib import Path
from typing import Any

import yaml

_VOCABULARIES: dict[str, dict[str, Any]] = {}

DEFAULT_LANG = "uk"


def load_vocabularies(vocab_dir: Path) -> None:
    """Scan vocab_dir for *.yml files and register each by file stem.

    The first file in sorted order becomes the default language.
    """
    global DEFAULT_LANG
    _VOCABULARIES.clear()

    if not vocab_dir.exists():
        return

    for path in sorted(vocab_dir.glob("*.yml")):
        with open(path, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        _VOCABULARIES[path.stem] = data

    if _VOCABULARIES:
        DEFAULT_LANG = next(iter(_VOCABULARIES))


def get_vocab(lang_cookie: str | None) -> dict[str, Any]:
    """Return the vocabulary for the requested language, falling back to the default."""
    if not _VOCABULARIES:
        return {}
    if lang_cookie and lang_cookie in _VOCABULARIES:
        return _VOCABULARIES[lang_cookie]
    return _VOCABULARIES.get(DEFAULT_LANG, next(iter(_VOCABULARIES.values())))
