"""Fernet encryption for Google refresh tokens at rest."""

from cryptography.fernet import Fernet, InvalidToken

from submissions_checker.core.config import Settings
from submissions_checker.services.google.oauth import GoogleAuthError


def _fernet(settings: Settings) -> Fernet:
    key = settings.google_token_encryption_key
    if not key:
        raise GoogleAuthError("GOOGLE_TOKEN_ENCRYPTION_KEY is not configured")
    try:
        return Fernet(key.encode())
    except ValueError as exc:
        raise GoogleAuthError("GOOGLE_TOKEN_ENCRYPTION_KEY is not a valid Fernet key") from exc


def encrypt_token(settings: Settings, plaintext: str) -> str:
    return _fernet(settings).encrypt(plaintext.encode()).decode()


def decrypt_token(settings: Settings, ciphertext: str) -> str:
    try:
        return _fernet(settings).decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise GoogleAuthError("stored Google token cannot be decrypted") from exc
