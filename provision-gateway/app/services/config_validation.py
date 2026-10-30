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
    # 0. Sanity: the file must actually LOOK like an nginx configuration.
    # A proxy-target check alone accepted leaked tool-call markup (DeepSeek
    # ｜DSML｜ tokens) as "valid" and persisted it as the deployed conf.
    try:
        _text = p.read_text(errors="replace")
    except OSError as exc:
        return {"valid": False, "errors": [f"nginx conf unreadable: {exc}"], "skipped": False}
    if "server" not in _text and "location" not in _text:
        return {
            "valid": False,
            "errors": [
                "nginx conf contains no server{} / location block — "
                "not a valid nginx configuration (tool-call markup leak?)"
            ],
            "skipped": False,
        }
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
    """Completeness + line-sanity check for the generated env output.

    Every non-empty, non-comment line MUST look like KEY=VALUE — junk lines
    (e.g. inline ``<invoke …>`` tool-call XML that reasoning models emit in
    the content field) make ``docker compose --env-file`` fail at deploy time
    even when the completeness check passes.

    Returns ``{"valid", "errors", "missing": [str], "skipped"}``.
    """
    from pathlib import Path as _Path
    import tempfile

    errors: list[str] = []
    for raw in (env_content or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            errors.append(f"invalid env line (not KEY=VALUE): {line[:80]}")

    paths = [_Path(p) for p in compose_paths]
    scan = scan_compose_files(paths)
    missing: list[str] = []
    if scan.refs:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = _Path(tmp) / ".env"
            env_file.write_text(env_content or "", encoding="utf-8")
            missing = env_completeness(paths, [env_file])
        if missing:
            errors.append(f"missing env var(s) without defaults: {', '.join(missing)}")

    return {"valid": not errors, "errors": errors, "missing": missing, "skipped": False}


def _compose_profiles(compose_paths: list[str | Path]) -> set[str]:
    """Every profile ANY service in the given compose files declares."""
    import yaml

    out: set[str] = set()
    for p in compose_paths:
        try:
            data = yaml.safe_load(Path(p).read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        services = (data or {}).get("services") if isinstance(data, dict) else None
        if isinstance(services, dict):
            for svc in services.values():
                if isinstance(svc, dict):
                    for prof in svc.get("profiles") or []:
                        if isinstance(prof, str) and prof:
                            out.add(prof)
    return out


def validate_serving_ports(
    content: str, compose_paths: list[str | Path]
) -> dict[str, Any]:
    """Mechanical validation of a generated `serving_ports` artifact (T13/R1.2).

    The errors are the ones that make the artifact WRONG for the platform:

    * it does not parse, or has no `ports:` list;
    * a row is not a mapping, or misses `sub_service` / `port`;
    * a row names a service the compose does not have (the platform routes by
      that name);
    * `--` appears anywhere in a name — it is the platform's reserved separator
      and an ambiguous name cannot be split again (R1.5);
    * a `profiles` entry the compose never declares (the deploy panel can only
      select what exists).

    R14 — a `primary: true` field, when present in an older artifact, is
    IGNORED without a warning: every `kind: http` row is an equal endpoint at
    `<instance host>--<sub_service>.<domain>`, and the instance host is identity,
    not a route.

    Two things are deliberately WARNINGS, not errors, because the doc leaves them
    to the operator's judgment (T14's `validate_ports` phase is where a guided fix
    can pause): a port the compose mentions with no row (an omission worth
    surfacing, but a row list may be deliberately partial), and a row with no
    `kind` (legal, and NOT exposed — R1.2's fail-closed rule — so it is worth
    saying out loud without blocking).
    """
    import yaml

    from .file_sets import compose_service_names
    from .serving_ports import compose_port_surface

    errors: list[str] = []
    warnings: list[str] = []

    try:
        data = yaml.safe_load(content or "")
    except Exception as exc:  # noqa: BLE001 — any parse failure is the same answer
        return {"valid": False, "errors": [f"not valid YAML: {exc}"], "warnings": [], "repairable": True}

    if not isinstance(data, dict) or not isinstance(data.get("ports"), list):
        return {
            "valid": False,
            "errors": ["output must be a mapping with a `ports:` list"],
            "warnings": [],
            "repairable": True,
        }

    # `compose_service_names` is path-based (it reads the files); the context
    # hands us strings, so they are wrapped here — measured live 2026-10-10:
    # passing strings raised `'str' object has no attribute 'is_file'` and the
    # generation job failed outright.
    services = set(compose_service_names([Path(p) for p in compose_paths]))
    declared_profiles = set(_compose_profiles(compose_paths))
    seen_ports = {(r.get("sub_service"), r.get("port")) for r in compose_port_surface([Path(p) for p in compose_paths])}

    for i, row in enumerate(data["ports"]):
        where = f"ports[{i}]"
        if not isinstance(row, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        sub = row.get("sub_service")
        port = row.get("port")
        if not isinstance(sub, str) or not sub.strip():
            errors.append(f"{where}: `sub_service` must be a non-empty string")
        elif "--" in sub:
            errors.append(f"{where}: `sub_service` must not contain '--' (reserved separator)")
        elif services and sub not in services:
            errors.append(f"{where}: `sub_service` {sub!r} is not a service in the compose")
        if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
            errors.append(f"{where}: `port` must be an integer in 1..65535")
        kind = row.get("kind")
        if kind is not None and (not isinstance(kind, str) or not kind.strip()):
            errors.append(f"{where}: `kind`, when present, must be a non-empty string")
        elif kind is None:
            warnings.append(f"{where}: no `kind` — this row is NOT exposed (fail-closed)")
        profiles = row.get("profiles") or []
        if not isinstance(profiles, list):
            errors.append(f"{where}: `profiles` must be a list")
        else:
            for p in profiles:
                if not isinstance(p, str):
                    errors.append(f"{where}: `profiles` entries must be strings")
                elif declared_profiles and p not in declared_profiles:
                    errors.append(f"{where}: profile {p!r} is not declared by the compose")

    for sub, port in sorted(seen_ports, key=lambda x: (str(x[0]), str(x[1]))):
        if (sub, port) not in {(r.get("sub_service"), r.get("port")) for r in data["ports"]}:
            warnings.append(f"the compose mentions {sub}:{port} and no row declares it")

    return {"valid": not errors, "errors": errors, "warnings": warnings, "repairable": True}
