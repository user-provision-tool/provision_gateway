"""Local-only first-admin bootstrap (T6 step 7 / R5.2).

    python -m app.bootstrap_admin [--username admin] [--password ...]

Runs only when no live admin exists; creates exactly one approved/active admin,
ensures its api_users row through the -api, and opens registration. There is no
HTTP route for this. When no password is supplied a one-time password is
written ONLY to ``PROVISION_DIR/gateway_data/bootstrap_admin.password`` (0600),
never to stdout/container logs; the operator rotates it after first login.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

from .config import settings
from .database import SessionLocal, init_db
from .services import account_service


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Create the first admin (local only)")
    ap.add_argument("--username", default=os.environ.get("BOOTSTRAP_ADMIN_USERNAME", "admin"))
    ap.add_argument("--password", default=None)
    ap.add_argument("--email", default=None)
    args = ap.parse_args(argv)
    password = args.password or os.environ.get("BOOTSTRAP_ADMIN_PASSWORD")
    generated = password is None
    if generated:
        password = secrets.token_urlsafe(18)

    init_db()
    db = SessionLocal()
    try:
        admin = account_service.bootstrap_admin(
            db, username=args.username, password=password, email=args.email
        )
        print(f"[bootstrap] created admin '{args.username}' ({admin.id})")
    except ValueError as exc:
        print(f"[bootstrap] refused: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    if generated:
        path = Path(settings.GATEWAY_DATA_DIR) / "bootstrap_admin.password"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(password + "\n")
        os.chmod(path, 0o600)
        print(f"[bootstrap] one-time password written to {path} (rotate after first login)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
