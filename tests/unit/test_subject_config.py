"""Unit tests for services.subject_config — pure YAML config parsing."""

from __future__ import annotations

import pytest
import yaml

from submissions_checker.services.subject_config import load_config, parse_config


def test_parse_config_basic() -> None:
    raw = b"subjectCode: demo\nname: Demo\nassignments:\n  lab1:\n    title: One\n"
    cfg = parse_config(raw)
    assert cfg["subjectCode"] == "demo"
    assert cfg["name"] == "Demo"
    assert cfg["assignments"]["lab1"]["title"] == "One"


def test_parse_config_handles_utf8() -> None:
    cfg = parse_config("name: Matemática 数学\n".encode())
    assert cfg["name"] == "Matemática 数学"


def test_parse_config_empty_is_none() -> None:
    assert parse_config(b"") is None


def test_parse_config_invalid_yaml_raises() -> None:
    with pytest.raises(yaml.YAMLError):
        parse_config(b"key: [unterminated\n")


def test_load_config_reads_file(tmp_path) -> None:
    p = tmp_path / "config.yml"
    p.write_text("subjectCode: x\nfoo: 1\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg == {"subjectCode": "x", "foo": 1}


def test_load_config_accepts_str_path(tmp_path) -> None:
    p = tmp_path / "config.yml"
    p.write_text("a: b\n", encoding="utf-8")
    assert load_config(str(p)) == {"a": "b"}


# ── validate_subject_code ─────────────────────────────────────────────────────


@pytest.mark.parametrize("code", ["demo", "distributedBasics", "os-2026_v2", "a.b"])
def test_validate_subject_code_accepts_plain_identifiers(code: str) -> None:
    from submissions_checker.services.subject_config import validate_subject_code

    assert validate_subject_code(code) == code


@pytest.mark.parametrize(
    "code",
    ["", "../templates", "a/b", "a\\b", ".hidden", "..", "x..y", "a b", "ü", "x" * 65],
)
def test_validate_subject_code_rejects_path_like_values(code: str) -> None:
    from submissions_checker.services.subject_config import validate_subject_code

    with pytest.raises(ValueError):
        validate_subject_code(code)
