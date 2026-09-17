"""Secrets that must sit in a database row for a while are sealed with the app key."""

from __future__ import annotations

import pytest

from submissions_checker.core import sealed


def test_roundtrip(monkeypatch) -> None:
    monkeypatch.setattr(sealed, "_secret_key", lambda: "k" * 40)
    token = sealed.seal("s3cret-pw")
    assert "s3cret-pw" not in token
    assert sealed.unseal(token) == "s3cret-pw"


def test_other_key_cannot_open(monkeypatch) -> None:
    monkeypatch.setattr(sealed, "_secret_key", lambda: "a" * 40)
    token = sealed.seal("pw")
    monkeypatch.setattr(sealed, "_secret_key", lambda: "b" * 40)
    with pytest.raises(ValueError):
        sealed.unseal(token)


def test_tampered_token_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(sealed, "_secret_key", lambda: "k" * 40)
    token = sealed.seal("pw")
    with pytest.raises(ValueError):
        sealed.unseal(token[:-2] + "zz")
    with pytest.raises(ValueError):
        sealed.unseal("not-a-token")


def test_is_sealed_recognises_own_tokens(monkeypatch) -> None:
    monkeypatch.setattr(sealed, "_secret_key", lambda: "k" * 40)
    assert sealed.is_sealed(sealed.seal("pw"))
    assert not sealed.is_sealed("<sent>")
    assert not sealed.is_sealed("plaintext")
