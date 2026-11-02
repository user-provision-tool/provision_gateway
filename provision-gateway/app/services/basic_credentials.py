"""Service Basic credential read (T9/R2.5).

The gateway only READS the keyring the -api owns. It never writes or replaces
it. A missing key, a wrong key, a tombstoned/absent row or a `reset_required`
(bcrypt-only) row all yield no credential — Basic injection is disabled rather
than guessed.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text

from ..config import settings
from ..database import reader_engine


def _keyring_path() -> Path:
    return settings.PROVISION_DIR / "keys" / "service_basic.key"


def _load_key(version: str) -> bytes | None:
    try:
        ring = json.loads(_keyring_path().read_text(encoding="utf-8"))
        raw = base64.b64decode(ring["keys"][version])
        return raw if len(raw) == 32 else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def decrypt(envelope: bytes | str | None) -> str | None:
    if envelope is None:
        return None
    if isinstance(envelope, bytes):
        text_value = envelope.decode("utf-8")
    else:
        text_value = envelope
    try:
        marker, nonce_b64, ct_b64 = text_value.split(":", 2)
        if not marker.startswith("v"):
            return None
        key = _load_key(marker[1:])
        if key is None:
            return None
        return AESGCM(key).decrypt(
            base64.b64decode(nonce_b64), base64.b64decode(ct_b64), None
        ).decode("utf-8")
    except Exception:
        return None


def get_basic(service_id: str) -> tuple[str, str] | None:
    """Return ``(basic_username, password)`` or None (fail closed)."""
    with reader_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT basic_username, secret_enc, reset_required "
                "FROM registry.service_basic WHERE service_id=:id"
            ),
            {"id": service_id},
        ).mappings().first()
    if not row or row["reset_required"] or row["secret_enc"] is None:
        return None
    plain = decrypt(bytes(row["secret_enc"]))
    if plain is None:
        return None
    return row["basic_username"], plain


def authorization_header(service_id: str) -> str:
    """The `X-Service-Basic` value, or '' when Basic is not usable."""
    basic = get_basic(service_id)
    if basic is None:
        return ""
    username, password = basic
    return base64.b64encode(f"{username}:{password}".encode()).decode()
