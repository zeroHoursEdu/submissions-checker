"""Unit tests for the i18n vocabulary loader / language resolver.

Pure filesystem + YAML; no network. Uses tmp_path for vocab dirs and resets the
module-global state per test so cases don't leak into each other.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from submissions_checker.core import i18n


@pytest.fixture(autouse=True)
def _reset_i18n_state():
    # Snapshot + restore the module globals so tests are isolated.
    saved_vocab = dict(i18n._VOCABULARIES)
    saved_langs = list(i18n.AVAILABLE_LANGUAGES)
    saved_default = i18n.DEFAULT_LANG
    yield
    i18n._VOCABULARIES.clear()
    i18n._VOCABULARIES.update(saved_vocab)
    i18n.AVAILABLE_LANGUAGES.clear()
    i18n.AVAILABLE_LANGUAGES.extend(saved_langs)
    i18n.DEFAULT_LANG = saved_default


def _write(dir_: Path, name: str, content: str) -> None:
    (dir_ / name).write_text(content, encoding="utf-8")


def test_load_vocabularies_missing_dir_is_noop(tmp_path: Path) -> None:
    i18n._VOCABULARIES["stale"] = {}
    i18n.AVAILABLE_LANGUAGES.append({"code": "stale", "label": "x"})
    i18n.load_vocabularies(tmp_path / "does-not-exist")
    # cleared even though dir is missing
    assert i18n._VOCABULARIES == {}
    assert i18n.AVAILABLE_LANGUAGES == []


def test_load_vocabularies_registers_languages_and_labels(tmp_path: Path) -> None:
    _write(tmp_path, "en.yml", "_meta:\n  label: English\nhello: Hi\n")
    _write(tmp_path, "uk.yml", "_meta:\n  label: Ukrainian\nhello: Pryvit\n")

    i18n.load_vocabularies(tmp_path)

    codes = {l["code"] for l in i18n.AVAILABLE_LANGUAGES}
    assert codes == {"en", "uk"}
    labels = {l["code"]: l["label"] for l in i18n.AVAILABLE_LANGUAGES}
    assert labels["en"] == "English"
    assert labels["uk"] == "Ukrainian"
    # first sorted file (en) becomes the default
    assert i18n.DEFAULT_LANG == "en"


def test_load_vocabularies_label_falls_back_to_code(tmp_path: Path) -> None:
    _write(tmp_path, "de.yml", "hello: Hallo\n")  # no _meta
    i18n.load_vocabularies(tmp_path)
    assert i18n.AVAILABLE_LANGUAGES == [{"code": "de", "label": "de"}]


def test_load_vocabularies_empty_file_yields_empty_dict(tmp_path: Path) -> None:
    _write(tmp_path, "fr.yml", "")  # yaml.safe_load -> None -> {}
    i18n.load_vocabularies(tmp_path)
    assert i18n._VOCABULARIES["fr"] == {}


def test_get_vocab_no_vocabularies_returns_empty() -> None:
    i18n._VOCABULARIES.clear()
    assert i18n.get_vocab("en") == {}


def test_get_vocab_returns_requested_language(tmp_path: Path) -> None:
    _write(tmp_path, "en.yml", "_meta:\n  label: English\nhello: Hi\n")
    _write(tmp_path, "uk.yml", "_meta:\n  label: Ukrainian\nhello: Pryvit\n")
    i18n.load_vocabularies(tmp_path)
    assert i18n.get_vocab("uk")["hello"] == "Pryvit"


def test_get_vocab_unknown_or_none_falls_back_to_default(tmp_path: Path) -> None:
    _write(tmp_path, "en.yml", "hello: Hi\n")
    _write(tmp_path, "uk.yml", "hello: Pryvit\n")
    i18n.load_vocabularies(tmp_path)  # default == en (sorted first)
    assert i18n.get_vocab(None)["hello"] == "Hi"
    assert i18n.get_vocab("zz")["hello"] == "Hi"
