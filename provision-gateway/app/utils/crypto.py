"""AES-256-GCM encryption for BYOK API keys."""

from __future__ import annotations

import os
from base64 import b64encode, b64decode

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..config import settings


def _derive_key() -> bytes:
    """Derive a 32-byte AES key from GATEWAY_SECRET_KEY.
    
    Uses SHA-256 to ensure consistent 32-byte key regardless of
    the secret key length.
    """
    import hashlib
    return hashlib.sha256(settings.GATEWAY_SECRET_KEY.encode("utf-8")).digest()


def encrypt_api_key(plaintext: str) -> str:
    """Encrypt an API key using AES-256-GCM.
    
    Returns a base64-encoded string containing nonce + ciphertext.
    """
    if not plaintext:
        return ""
    key = _derive_key()
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    # Prepend nonce to ciphertext, then base64 encode
    combined = nonce + ciphertext
    return b64encode(combined).decode("utf-8")


def decrypt_api_key(encrypted: str) -> str:
    """Decrypt an AES-256-GCM encrypted API key.
    
    Expects a base64-encoded string containing nonce + ciphertext.
    Returns empty string if input is empty.
    """
    if not encrypted:
        return ""
    key = _derive_key()
    try:
        combined = b64decode(encrypted.encode("utf-8"))
        nonce = combined[:12]
        ciphertext = combined[12:]
        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        return plaintext.decode("utf-8")
    except Exception:
        return ""
