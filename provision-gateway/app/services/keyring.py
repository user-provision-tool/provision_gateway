"""Read-only keyring access for the gateway's own encrypted options (T8).

The -api creates and owns `PROVISION_DIR/keys/service_basic.key`; the gateway
reads it and may ENCRYPT values destined for its own
`pre_deploy_sessions.options_secret_enc`. It never writes or replaces the file.
"""

from __future__ import annotations

import base64
import json
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..config import settings

KEY_BYTES = 32
NONCE_BYTES = 12


class KeyringUnavailable(RuntimeError):
    pass


def _path() -> Path:
    return Path(settings.PROVISION_DIR) / "keys" / "service_basic.key"


def read() -> dict:
    try:
        ring = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise KeyringUnavailable(f"service Basic keyring unavailable: {exc}") from exc
    if not isinstance(ring, dict) or not ring.get("keys") or not ring.get("current"):
        raise KeyringUnavailable("service Basic keyring is malformed")
    return ring


def _key(ring: dict, version: str) -> bytes:
    raw = base64.b64decode((ring.get("keys") or {})[version])
    if len(raw) != KEY_BYTES:
        raise KeyringUnavailable(f"key version {version!r} is not 32 bytes")
    return raw


def encrypt(plaintext: str, version: str | None = None) -> bytes:
    ring = read()
    ver = version or ring["current"]
    nonce = secrets.token_bytes(NONCE_BYTES)
    ct = AESGCM(_key(ring, ver)).encrypt(nonce, plaintext.encode("utf-8"), None)
    return f"v{ver}:{base64.b64encode(nonce).decode()}:{base64.b64encode(ct).decode()}".encode()


def decrypt(envelope: bytes | str | None) -> str | None:
    if envelope is None:
        return None
    value = envelope.decode("utf-8") if isinstance(envelope, bytes) else envelope
    try:
        marker, nonce_b64, ct_b64 = value.split(":", 2)
        ring = read()
        return AESGCM(_key(ring, marker[1:])).decrypt(
            base64.b64decode(nonce_b64), base64.b64decode(ct_b64), None
        ).decode("utf-8")
    except Exception:
        return None
