"""Encrypt/decrypt the frozen rollback copies (T12/R8.6).

    ROLLBACK_ENCRYPTION_KEY=<base64 32 bytes> \
      python -m app.tools.rollback_crypt encrypt <in> <out>
    ... decrypt <in> <out>

Envelope: ``v<key_id>:<base64 nonce>:<base64 ciphertext>`` (AES-GCM). The key is
held outside the repo and stored separately from the copies — never in the same
archive. Missing/wrong key fails closed.
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_ID = "1"
NONCE = 12


def _key() -> bytes:
    raw = os.environ.get("ROLLBACK_ENCRYPTION_KEY", "").strip()
    if not raw:
        raise SystemExit("ROLLBACK_ENCRYPTION_KEY is required")
    try:
        key = base64.b64decode(raw)
    except ValueError as exc:
        raise SystemExit("ROLLBACK_ENCRYPTION_KEY is not valid base64") from exc
    if len(key) != 32:
        raise SystemExit("ROLLBACK_ENCRYPTION_KEY must be 32 bytes (base64)")
    return key


def encrypt_file(src: Path, dst: Path) -> None:
    nonce = secrets.token_bytes(NONCE)
    ct = AESGCM(_key()).encrypt(nonce, src.read_bytes(), None)
    envelope = f"v{KEY_ID}:{base64.b64encode(nonce).decode()}:{base64.b64encode(ct).decode()}"
    dst.write_text(envelope, encoding="utf-8")
    os.chmod(dst, 0o600)


def decrypt_file(src: Path, dst: Path) -> None:
    marker, nonce_b64, ct_b64 = src.read_text(encoding="utf-8").split(":", 2)
    if marker != f"v{KEY_ID}":
        raise SystemExit(f"unknown key id {marker!r}")
    data = AESGCM(_key()).decrypt(
        base64.b64decode(nonce_b64), base64.b64decode(ct_b64), None
    )
    dst.write_bytes(data)
    os.chmod(dst, 0o600)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=("encrypt", "decrypt"))
    ap.add_argument("src")
    ap.add_argument("dst")
    args = ap.parse_args(argv)
    src, dst = Path(args.src), Path(args.dst)
    if not src.is_file():
        raise SystemExit(f"source not found: {src}")
    (encrypt_file if args.action == "encrypt" else decrypt_file)(src, dst)
    print(f"{args.action}: {src} -> {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
