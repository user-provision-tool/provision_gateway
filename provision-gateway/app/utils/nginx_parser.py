"""Nginx config parser — extracts upstreams and server names."""

from __future__ import annotations

import re
from pathlib import Path


def parse_nginx_conf(filepath: str | Path) -> dict:
    """Parse an nginx config file and extract key directives.
    
    Returns dict with:
        - server_name: str | None
        - proxy_pass: str | None
        - listen: list[str]
        - auth_basic: bool
        - ssl_enabled: bool
    """
    content = Path(filepath).read_text()
    result = {
        "server_name": None,
        "proxy_pass": None,
        "listen": [],
        "auth_basic": False,
        "ssl_enabled": False,
    }

    m = re.search(r"server_name\s+([^;]+);", content)
    if m:
        result["server_name"] = m.group(1).strip().split()[0]

    m = re.search(r"proxy_pass\s+(https?://[^;]+);", content)
    if m:
        result["proxy_pass"] = m.group(1).strip()

    for m in re.finditer(r"listen\s+([^;]+);", content):
        result["listen"].append(m.group(1).strip())

    result["auth_basic"] = "auth_basic" in content
    result["ssl_enabled"] = "ssl_certificate" in content

    return result


def extract_upstream_target(proxy_pass: str) -> tuple[str, int]:
    """Extract container name and port from a proxy_pass URL.
    
    Example: 'http://myapp-web:8000' → ('myapp-web', 8000)
    """
    m = re.match(r"https?://([^:]+)(?::(\d+))?", proxy_pass)
    if m:
        name = m.group(1)
        port = int(m.group(2)) if m.group(2) else 80
        return name, port
    return proxy_pass, 80
