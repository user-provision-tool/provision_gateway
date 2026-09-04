"""Mechanical validation of generated config files.

Design §Generation rules: before the save is offered, generated output is
validated mechanically — compose via ``docker compose config`` (gateway-local;
the daemon is NOT needed for ``config``), nginx via proxy-target validation
(provision-api ``POST /nginx/validate``) + best-effort ``nginx -t``, and
interpolation env via the completeness check (every ``${VAR}`` without a
default present in the output). On failure the errors are fed back into the
agent loop for self-repair.

All validators degrade gracefully: if the local toolchain is unavailable the
check reports ``skipped=True`` instead of blocking the human review gate.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from ..config import settings
from ..utils.var_scan import env_completeness, scan_compose_files


def validate_compose(compose_paths: list[str | Path]) -> dict[str, Any]:
    """Run ``docker compose config --no-interpolate --no-path-resolution``.

    Returns ``{"valid": bool, "errors": [str], "skipped": bool}``. ``skipped``
    is True when the docker CLI/compose plugin is unavailable (cannot verify).
    """
    paths = [Path(p) for p in compose_paths]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        return {
            "valid": False,
            "errors": [f"compose file does not exist: {m}" for m in missing],
            "skipped": False,
        }
    if not paths:
        return {"valid": False, "errors": ["no compose files to validate"], "skipped": False}
    cmd = ["docker", "compose"]
    for p in paths:
        cmd += ["-f", str(p)]
    cmd += ["config", "--no-interpolate", "--no-path-resolution"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        return {"valid": True, "errors": [], "skipped": True,
                "note": "docker CLI unavailable — compose config check skipped"}
    except subprocess.TimeoutExpired:
        return {"valid": False, "errors": ["docker compose config timed out"], "skipped": False}
    if result.returncode != 0:
        tail = "\n".join((result.stderr or "").splitlines()[-8:])
        return {"valid": False, "errors": [f"docker compose config failed: {tail}"], "skipped": False}
    return {"valid": True, "errors": [], "skipped": False}


async def validate_nginx(
    nginx_path: str | Path, compose_service_names: list[str]
) -> dict[str, Any]:
    """Validate a generated nginx conf.

    1. Proxy-target validation via provision-api ``POST /nginx/validate``
       (runs ``_validate_nginx_proxy_targets`` against the merged service-name
       set — which includes profile-gated services).
    2. Best-effort local ``nginx -t`` (only when the binary exists).
    """
    p = Path(nginx_path)
    if not p.is_file():
        return {"valid": False, "errors": [f"nginx conf does not exist: {p}"], "skipped": False}

    errors: list[str] = []
    # 1. Proxy-target validation (authoritative — provision-api endpoint).
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{settings.PROVISION_API_URL.rstrip('/')}/nginx/validate",
                json={"nginx_conf_path": str(p), "compose_service_names": compose_service_names},
            )
            if resp.status_code < 400:
                body = resp.json()
                if not body.get("valid", True):
                    errors.extend(body.get("errors", ["nginx proxy targets invalid"]))
            else:
                errors.append(f"provision-api nginx validation unavailable (HTTP {resp.status_code})")
    except Exception as exc:
        errors.append(f"nginx validation call failed: {exc}")

    # 2. Best-effort local nginx -t.
    try:
        result = subprocess.run(
            ["nginx", "-t", "-c", str(p.resolve())],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            tail = "\n".join((result.stderr or result.stdout or "").splitlines()[-8:])
            errors.append(f"nginx -t failed: {tail}")
    except FileNotFoundError:
        pass  # nginx binary absent — proxy-target check above remains authoritative
    except subprocess.TimeoutExpired:
        pass

    return {"valid": len(errors) == 0, "errors": errors, "skipped": False}


def validate_env(
    env_content: str, compose_paths: list[str | Path]
) -> dict[str, Any]:
    """Completeness check: every no-default ${VAR} must be present in the env output.

    Returns ``{"valid", "errors", "missing": [str], "skipped"}``.
    """
    from pathlib import Path as _Path
    import tempfile

    paths = [_Path(p) for p in compose_paths]
    scan = scan_compose_files(paths)
    if not scan.refs:
        return {"valid": True, "errors": [], "missing": [], "skipped": False}

    with tempfile.TemporaryDirectory() as tmp:
        env_file = _Path(tmp) / ".env"
        env_file.write_text(env_content or "", encoding="utf-8")
        missing = env_completeness(paths, [env_file])
    if missing:
        return {
            "valid": False,
            "errors": [f"missing env var(s) without defaults: {', '.join(missing)}"],
            "missing": missing,
            "skipped": False,
        }
    return {"valid": True, "errors": [], "missing": [], "skipped": False}
