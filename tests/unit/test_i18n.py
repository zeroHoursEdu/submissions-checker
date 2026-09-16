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
    saved_default = i18n.DEFAULT_LANG
    yield
    i18n._VOCABULARIES.clear()
    i18n._VOCABULARIES.update(saved_vocab)
    i18n.DEFAULT_LANG = saved_default


def _write(dir_: Path, name: str, content: str) -> None:
    (dir_ / name).write_text(content, encoding="utf-8")


def test_load_vocabularies_missing_dir_is_noop(tmp_path: Path) -> None:
    i18n._VOCABULARIES["stale"] = {}
    i18n.load_vocabularies(tmp_path / "does-not-exist")
    # cleared even though dir is missing
    assert i18n._VOCABULARIES == {}


def test_load_vocabularies_registers_languages(tmp_path: Path) -> None:
    _write(tmp_path, "uk.yml", "nav:\n  x: y\n")
    _write(tmp_path, "en.yml", "nav:\n  x: z\n")

    i18n.load_vocabularies(tmp_path)

    assert i18n.get_vocab("uk")["nav"]["x"] == "y"
    assert i18n.get_vocab("en")["nav"]["x"] == "z"
    # first sorted file (en) becomes the default
    assert i18n.DEFAULT_LANG == "en"


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


def test_shipped_vocabularies_have_only_string_keys() -> None:
    """Guard against YAML's bare-keyword keys silently blanking a template lookup.

    ``true:``/``false:``/``yes:``/``no:``/``on:``/``off:``/``null:`` written unquoted parse
    as booleans or None, not strings. Jinja then resolves ``vocab.common.true`` to Undefined
    and renders an empty string — which is how every True/False quiz question shipped with
    two unlabelled radio buttons.
    """
    i18n.load_vocabularies(Path("i18n"))
    assert i18n._VOCABULARIES, "no vocabularies loaded from i18n/"

    offenders: list[str] = []

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if not isinstance(key, str):
                    offenders.append(f"{path}.{key!r} ({type(key).__name__})")
                walk(value, f"{path}.{key}")

    for lang, vocab in i18n._VOCABULARIES.items():
        walk(vocab, lang)

    assert offenders == []


def test_shipped_vocabularies_define_the_true_false_labels() -> None:
    """The True/False quiz renderer reads these two keys directly."""
    i18n.load_vocabularies(Path("i18n"))
    for lang, vocab in i18n._VOCABULARIES.items():
        assert vocab["common"]["true"], f"{lang} is missing common.true"
        assert vocab["common"]["false"], f"{lang} is missing common.false"
