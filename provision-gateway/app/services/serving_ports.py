"""The `serving_ports` artifact's semantics — the kind table, the category rule
and the port surface of a compose.

Three consumers, one definition each:

* the GENERATION prompt quotes the table and the port surface (T13),
* the VALIDATION phases check a produced artifact against the port surface
  (T14), and
* whatever renders a conf asks :func:`category_of` which kind of listener a row
  belongs to (T9's generator, T15's picker).

**The kind is not the port.** A port number is not authoritative — applications
remap ports (Bolt on 17373 is as real as Bolt on 7687) — so the kind is inferred
by the LLM from the compose's *context*, and this module deliberately never
derives a kind from a number. The one table that IS load-bearing is the preset
`kind → category` map, which lives as a config file beside the generation skill
(`app/skills/provision-api/kind-categories.yml`) because editing it is
platform-level policy (GRILLING G37 / task-doc R1.2).
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

_KIND_TABLE_PATH = (
    Path(__file__).resolve().parent.parent / "skills" / "provision-api" / "kind-categories.yml"
)

# The categories this platform knows. Only `stream` is served by the exposure-URL
# feature today; `http` and `mail` name where their confs would go.
CATEGORIES = ("http", "mail", "stream")

_lock = threading.Lock()
_cache: dict[str, Any] | None = None
_cache_mtime: float = 0.0

# The preset, used when the config file is absent or unreadable. It is the same
# table the file carries — a missing config must not silently change what is
# exposed, and it must not fail a deploy either.
_PRESET = {
    "http": "http", "https": "http", "ws": "http", "wss": "http",
    "grpc": "http", "http2": "http", "h2c": "http",
    "smtp": "mail", "smtps": "mail", "submission": "mail",
    "imap": "mail", "imaps": "mail", "pop3": "mail", "pop3s": "mail",
}
_PRESET_ELSE = "stream"


def load_kind_categories() -> dict[str, Any]:
    """The `kind → category` table: ``{"kinds": {...}, "else": "<category>"}``.

    Re-read when the file changes, like the registry wrapper — the table is
    platform policy and an edit must take effect without a restart.
    """
    global _cache, _cache_mtime
    try:
        if not _KIND_TABLE_PATH.exists():
            return {"kinds": dict(_PRESET), "else": _PRESET_ELSE}
        mtime = _KIND_TABLE_PATH.stat().st_mtime
    except OSError:
        return {"kinds": dict(_PRESET), "else": _PRESET_ELSE}

    if _cache is not None and mtime <= _cache_mtime:
        return _cache
    with _lock:
        if _cache is not None and mtime <= _cache_mtime:
            return _cache
        try:
            import yaml as _yaml

            data = _yaml.safe_load(_KIND_TABLE_PATH.read_text(encoding="utf-8")) or {}
            kinds = {
                str(k).strip().lower(): str(v).strip().lower()
                for k, v in (data.get("kinds") or {}).items()
                if isinstance(k, str) and isinstance(v, str)
            }
            else_cat = str(data.get("else") or _PRESET_ELSE).strip().lower()
            table = {"kinds": kinds or dict(_PRESET), "else": else_cat}
        except Exception:
            table = {"kinds": dict(_PRESET), "else": _PRESET_ELSE}
        _cache, _cache_mtime = table, mtime
        return table


def category_of(kind: str | None) -> str | None:
    """The category a row's ``kind`` belongs to, or **None** when it has none.

    None is the fail-closed answer for task-doc R1.2: a row with no kind at all
    is not exposed. A kind that is named but unlisted takes the table's `else`
    (the default rule: everything unnamed is a stream protocol).
    """
    if not kind or not str(kind).strip():
        return None
    table = load_kind_categories()
    k = str(kind).strip().lower()
    return table["kinds"].get(k) or table["else"]


def is_exposed(kind: str | None) -> bool:
    """A row is exposed iff it has a kind (R1.2's fail-closed rule)."""
    return category_of(kind) is not None


def compose_port_surface(compose_paths: list[Path]) -> list[dict[str, Any]]:
    """Every port the given compose files mention, per service.

    ``ports:`` **and** ``expose:`` — the artifact lists all of them (R1.1), and
    the reference in the prompt is only as good as this surface. Read from the
    compose the deploy used: the *rendered* per-user compose has already had its
    ``ports:`` stripped by the converter, so it can never be the source.

    Best-effort by design: a malformed compose yields what could be read, because
    this feeds a prompt and a warning — never a hard failure of the run.
    """
    out: list[dict[str, Any]] = []
    for p in compose_paths:
        try:
            if not Path(p).is_file():
                continue
            import yaml as _yaml

            data = _yaml.safe_load(Path(p).read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        services = (data or {}).get("services") if isinstance(data, dict) else None
        if not isinstance(services, dict):
            continue
        for svc_name, svc in services.items():
            if not isinstance(svc, dict):
                continue
            for key, source in (("ports", "ports"), ("expose", "expose")):
                for entry in svc.get(key) or []:
                    port = _container_port(entry)
                    if port is None:
                        continue
                    row = {"sub_service": svc_name, "port": port, "source": source}
                    if row not in out:
                        out.append(row)
    return out


#: `${VAR:-default}` / `${VAR-default}` / `${VAR}` — compose's own substitution
#: forms, as docker compose resolves them.
_ENV_SUBST = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-?([^}]*))?\}")


def _expand_env_defaults(raw: str) -> str:
    """Resolve a compose `${VAR…}` substitution to its **default**.

    R2-F13 — measured 2026-10-10 while running the end-to-end plan: graphiti's
    compose declares its neo4j ports as

        expose:
          - "${NEO4J_PORT_1:-7474}"
          - "${NEO4J_PORT_2:-7687}"

    and `_container_port` returned None for both, so the mechanical surface handed
    to the prompt listed only graph's and falkordb's ports. The prompt says that
    surface is ground truth, so the model — correctly — reported "neo4j ports not
    listed" and omitted them. The artifact then had **no stream row for Bolt
    7687**, which is exactly the row this whole feature exists to expose, and the
    same omission had already been seen and recorded as an LLM mistake (round-1
    F28). It is not an LLM mistake: `${VAR:-default}` is an ordinary compose
    idiom, and a scanner that cannot read it silently deletes ports from the
    platform's inventory.

    The default is the right reading here: the scanner describes what the compose
    declares, and every consumer downstream (the converter's envsubst) uses the
    same default when the variable is unset.
    """
    if "${" not in raw:
        return raw
    return _ENV_SUBST.sub(lambda m: m.group(2) or "", raw)


def _container_port(entry: Any) -> int | None:
    """The CONTAINER-side port of one compose port/expose entry.

    Compose writes ports in several shapes — ``8000``, ``"8000"``,
    ``"8080:80"``, ``"127.0.0.1:8080:80"``, a long-form mapping, or a
    `${VAR:-default}` substitution — and the one that matters here is the
    container's, since that is what the platform must proxy to. A range
    (``"8000-8005"``) is reported by its first port: the artifact's row shape
    carries one port per row, and the LLM is asked to expand a range it considers
    meaningful.
    """
    if isinstance(entry, int):
        return entry
    if isinstance(entry, dict):
        for key in ("target", "published"):
            v = entry.get(key)
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.split("-")[0].strip().isdigit():
                return int(v.split("-")[0])
        return None
    if not isinstance(entry, str):
        return None
    raw = _expand_env_defaults(entry.strip())
    if not raw:
        return None
    tail = raw.split(":")[-1]
    head = tail.split("-")[0].strip()
    if head.isdigit():
        return int(head)
    if raw.split("-")[0].strip().isdigit():
        return int(raw.split("-")[0])
    return None


# ---------------------------------------------------------------------------
# R1.5 — the served name of an extra HTTP endpoint
# ---------------------------------------------------------------------------

# Reserved as the instance/sub-service separator. It must never appear INSIDE a
# component name, or the split that turns a served name back into
# (instance, sub-service) becomes ambiguous — and that split is what the card,
# the gateway's hostname index and a reader all rely on.
RESERVED_SEPARATOR = "--"


def contains_reserved(name: str | None) -> bool:
    """True when a component name carries the reserved separator."""
    return bool(name) and RESERVED_SEPARATOR in str(name)


def extra_http_host(*, service_name: str, user_name: str, label: str, domain: str,
                    sub_service: str) -> str:
    """`<instance host>--<sub_service>.<domain>` — one label before the domain.

    Every `kind: http` row beyond the primary is served here (R1.5), the primary
    keeping the instance's own hostname. Deliberately the same
    `<instance host>--<sub_service>` form the stream path uses as its canonical
    name, so ONE name identifies a sub-service on both paths — and like every
    other platform name it is exactly one label under the domain, which is what
    lets the single wildcard certificate serve it.
    """
    host = f"{service_name}-{user_name}-{label}"
    if contains_reserved(host) or contains_reserved(sub_service):
        raise ValueError(
            f"'{RESERVED_SEPARATOR}' is reserved and must not appear in an instance "
            f"identity or a sub-service name (got {host!r} / {sub_service!r})"
        )
    return f"{host}{RESERVED_SEPARATOR}{sub_service}.{domain}"


# ---------------------------------------------------------------------------
# T14 / R1.3 — the conf↔ports agreement (the `validate_conf` step)
# ---------------------------------------------------------------------------


def http_rows(serving_ports: Any) -> list[dict[str, Any]]:
    """The rows whose kind resolves to the `http` category."""
    if not isinstance(serving_ports, dict):
        return []
    rows = serving_ports.get("ports")
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and category_of(r.get("kind")) == "http"]


def _server_blocks(conf_text: str) -> list[str]:
    """The `server { … }` blocks of an nginx conf, by brace counting.

    Deliberately simple: this validates a conf the platform itself produced or the
    operator selected, and a full parser is not what the check needs — it needs to
    know which names are served and which upstreams are named.
    """
    blocks: list[str] = []
    depth = 0
    start = None
    for i, line in enumerate(conf_text.splitlines()):
        stripped = line.strip()
        if start is None and stripped.startswith("server") and "{" in stripped:
            start = i
        if start is not None:
            depth += stripped.count("{") - stripped.count("}")
            if depth <= 0 and "}" in stripped:
                blocks.append("\n".join(conf_text.splitlines()[start:i + 1]))
                start = None
    return blocks


def validate_conf_agreement(
    serving_ports: Any,
    conf_text: str,
    *,
    service_name: str,
    user_name: str,
    label: str,
    domain: str,
) -> dict[str, Any]:
    """Does the nginx conf agree with the ports file? (R1.3's `validate_conf`.)

    ERRORS, because each means the deployment does not match its own declaration:

    * a surviving `kind: http` row with no `server{}` block serving its name — the
      row says the endpoint exists and the conf serves nothing;
    * the primary's block does not serve the instance host — the instance's own
      URL would point somewhere else.

    WARNINGS, because the doc calls the last one "at least a warning": a block
    whose upstream names no row at all (the conf proxies something the artifact
    does not declare — worth saying, not worth blocking).

    The names follow R1.5: the primary is served at the instance host, every other
    http row at `<instance host>--<sub_service>.<domain>` — one name per
    sub-service, the same form the stream path uses.
    """
    import re

    errors: list[str] = []
    warnings: list[str] = []

    # R2-F46 — read the conf AS THE PLATFORM WILL RENDER IT.
    #
    # The conf in play is a TEMPLATE, and the -api's converter owns what it
    # becomes (`lib/nginx_converter.py`): a `server_name` token with no `--`
    # becomes the instance host, and one carrying R1.5's separator becomes
    # `<instance host>--<sub_service>.<domain>` — the extra endpoint, kept as one
    # label under the domain. The SKILL tells the generator to write
    # `<service>_hostname` / `<service>_hostname--<sub>` rather than the final
    # names, so judging the PRE-conversion text compares names the platform never
    # serves: it refused a generated conf for not serving a name it was about to
    # be given (measured 2026-10-10, jobs 366/367) — and, before the converter
    # learned the rule, it made every extra http row unservable (round 2's F47).
    #
    # The rewrite lives HERE, not at the caller, so no caller can forget it.
    instance_stem = f"{service_name}-{user_name}-{label}"
    instance_host = f"{instance_stem}.{domain}"

    def _normalise_server_names(match: "re.Match[str]") -> str:
        head, token, tail = match.group(1), match.group(2), match.group(3)
        served: list[str] = []
        for name in token.split():
            if "--" in name:
                # `<instance host>--<sub>.<domain>` (R1.5) — the STEM carries the
                # separator, so the result is still ONE label under the domain.
                sub = name.split("--", 1)[1].split(".")[0].strip()
                served.append(f"{instance_stem}--{sub}.{domain}" if sub else instance_host)
            else:
                served.append(instance_host)
        return head + " ".join(served) + tail

    conf_text = re.sub(
        r"([ \t]*server_name[ \t]+)([^\n;]+)(;)",
        _normalise_server_names,
        conf_text or "",
    )

    blocks = _server_blocks(conf_text or "")

    def _served_names(block: str) -> set[str]:
        names: set[str] = set()
        for m in re.finditer(r"^[ \t]*server_name[ \t]+([^;\n]+);", block, re.MULTILINE):
            names |= {t.strip() for t in m.group(1).split() if t.strip()}
        return names

    served: set[str] = set()
    upstreams: set[str] = set()
    for b in blocks:
        served |= _served_names(b)
        for m in re.finditer(r"^[ \t]*set[ \t]+\$[A-Za-z0-9_]+[ \t]+([A-Za-z0-9_.-]+):", b, re.MULTILINE):
            upstreams.add(m.group(1))
        for m in re.finditer(r"proxy_pass[ \t]+https?://([A-Za-z0-9_.-]+)", b):
            upstreams.add(m.group(1))

    # (`instance_host` was computed above, with the rendering normalisation.)
    rows = http_rows(serving_ports)
    primary = next((r for r in rows if r.get("primary") is True), None)
    declared_upstreams: set[str] = set()

    for row in rows:
        sub = str(row.get("sub_service") or "")
        if not sub:
            continue
        declared_upstreams.add(sub)
        want = (instance_host if row.get("primary") is True
                else extra_http_host(service_name=service_name, user_name=user_name,
                                     label=label, domain=domain, sub_service=sub))
        if want not in served:
            errors.append(
                f"row {sub}: no `server{{}}` block serves {want!r} — the row declares an "
                f"endpoint the conf does not serve"
            )

    if primary is not None and instance_host not in served:
        errors.append(
            f"the primary row's block does not serve the instance host {instance_host!r}"
        )

    for up in sorted(upstreams):
        if up not in declared_upstreams and up not in (instance_host,):
            warnings.append(
                f"a block proxies to {up!r}, which no row declares — declared: "
                f"{sorted(declared_upstreams) or 'nothing'}"
            )

    return {"valid": not errors, "errors": errors, "warnings": warnings}
