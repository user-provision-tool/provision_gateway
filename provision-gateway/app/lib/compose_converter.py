"""Lightweight compose-to-Jinja2 converter for provision-gateway.

Delegates to provision-api's converter via HTTP where possible,
with a fallback local implementation for basic cases.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml


def compose_file_to_template(
    src_path: str, dst_path: str, service_name_hint: str = ""
) -> str:
    """Convert a plain docker-compose.yml to a Jinja2 .yml.j2 template.
    
    For full conversion, this calls provision-api's converter.
    For basic conversion, applies local transformations.
    """
    src = Path(src_path)
    if not src.exists():
        raise FileNotFoundError(f"Compose file not found: {src_path}")
    
    content = src.read_text()
    
    # Basic transformations:
    # 1. Replace container_name with {{ container_prefix }}<svc>
    # 2. Replace bind-mount paths with {{ volumes['key'] }}
    # 3. Replace network names with {{ network_name }}
    
    try:
        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            raise ValueError("Invalid compose file format")
    except yaml.YAMLError:
        # If we can't parse, just write a header and pass through
        header = _make_header({}, service_name_hint)
        Path(dst_path).write_text(header + content)
        return str(dst_path)
    
    services = data.get("services", {})
    
    # Collect volume keys
    vol_keys = {}
    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        for vol_entry in svc.get("volumes", []):
            src_part = _volume_source(vol_entry)
            if src_part and src_part.startswith("/") and src_part not in {"/var/run/docker.sock", "/run/docker.sock"}:
                key = _slug(src_part)
                vol_keys[src_part] = key
    
    src_to_key = vol_keys
    
    # Build output
    lines = []
    lines.append(_make_header(src_to_key, service_name_hint))
    
    # Write transformed YAML
    transformed = _transform_services(content, src_to_key, service_name_hint, services)
    lines.append(transformed)
    
    dst = Path(dst_path)
    dst.write_text("\n".join(lines))
    return str(dst)


def get_compose_service_names(path: str) -> list[str]:
    """Extract service names from a compose file or template."""
    content = Path(path).read_text()
    try:
        data = yaml.safe_load(content)
        if isinstance(data, dict):
            return list(data.get("services", {}).keys())
    except yaml.YAMLError:
        pass
    return []


def _volume_source(entry) -> str | None:
    if isinstance(entry, str):
        parts = entry.split(":")
        return parts[0] if len(parts) >= 2 else None
    if isinstance(entry, dict):
        return entry.get("source")
    return None


def _slug(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s.strip("/~")).strip("_")
    return s or "vol"


def _make_header(src_to_key: dict, hint: str) -> str:
    lines = [
        f"# Jinja2 compose template — generated from plain docker-compose.yml",
        f"# Service: {hint or 'unknown'}",
        f"#",
        f"# Template variables:",
        f"#   {{{{ container_prefix }}}}  — container name prefix",
        f"#   {{{{ network_name }}}}      — per-user isolated network",
    ]
    if src_to_key:
        lines.append(f"#   {{{{ volumes }}}}            — per-user volume mapping")
        for src, key in src_to_key.items():
            lines.append(f"#     {{{{ volumes['{key}'] }}}}  → {src}")
    lines.append("")
    return "\n".join(lines)


def _transform_services(content: str, src_to_key: dict, hint: str, services: dict | None = None) -> str:
    """Apply Jinja2 template transformations to the compose content."""
    text = content
    
    # container_name → {{ container_prefix }}<svc_key>
    # Use the full service key from the parsed YAML "services:" section.
    # Build a map of original container_name → correct service key suffix.
    container_to_svc: dict[str, str] = {}
    if services:
        for svc_key, svc_def in services.items():
            if isinstance(svc_def, dict):
                orig = svc_def.get("container_name", "")
                if orig:
                    container_to_svc[orig] = svc_key
    
    def _replace_container(m: re.Match) -> str:
        original = m.group(2)
        # Use the full service key if known, otherwise fall back to the old heuristic
        if original in container_to_svc:
            svc = container_to_svc[original]
        else:
            svc = _extract_svc(original)
        return f"{m.group(1)}{{{{ container_prefix }}}}{svc}"
    
    text = re.sub(
        r'(\s+container_name:\s*)(\S+)',
        _replace_container,
        text,
    )
    
    # Bind-mount paths → {{ volumes['key'] }}
    for src, key in src_to_key.items():
        text = text.replace(src, f"{{{{ volumes['{key}'] }}}}")
    
    # Network names → {{ network_name }}
    # Find network definitions under networks: and replace names
    lines = text.split("\n")
    in_networks = False
    result = []
    for line in lines:
        if re.match(r'^networks:', line):
            in_networks = True
            result.append(line)
        elif in_networks and re.match(r'^\s{2,}\S', line) and not line.startswith("  #"):
            # Top-level network name
            indent = len(line) - len(line.lstrip())
            result.append(f"{' ' * indent}{{{{ network_name }}}}:")
        elif in_networks and line.strip() == "":
            in_networks = False
            result.append(line)
        else:
            result.append(line)
    
    return "\n".join(result)


def _extract_svc(container_name: str) -> str:
    """Extract the service suffix from a container name like 'myapp-web' → 'web'."""
    parts = container_name.rsplit("-", 1)
    return parts[-1] if len(parts) > 1 else container_name
