"""HostnameIndex — in-memory hostname-to-registry-entry lookup.

Gateway reads the registry YAML file via shared filesystem mount (no HTTP calls).
The index maps hostname strings (e.g. "myapp-alice-0.localhost") to
their corresponding registry entries, enabling fast /go/{hostname} resolution.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any


class HostnameIndex:
    """Thread-safe, in-memory hostname index backed by the registry YAML.

    On each lookup, the index is refreshed from disk if the file has changed.
    This gives eventual consistency without polling HTTP endpoints.
    """

    def __init__(self, registry_path: str) -> None:
        self._registry_path = Path(registry_path)
        self._lock = threading.Lock()
        self._index: dict[str, dict[str, Any]] = {}
        self._mtime: float = 0.0

    def _reload_if_stale(self) -> bool:
        """Reload index from disk if the YAML file has been modified. Thread-safe."""
        if not self._registry_path.exists():
            return False
        current_mtime = self._registry_path.stat().st_mtime
        if current_mtime <= self._mtime:
            return False
        with self._lock:
            # Double-check inside lock
            if current_mtime <= self._mtime:
                return False
            try:
                import yaml as _yaml
                with open(self._registry_path) as f:
                    data = _yaml.safe_load(f)
                entries = data if isinstance(data, list) else []
            except Exception:
                return False
            new_index: dict[str, dict[str, Any]] = {}
            for entry in entries:
                hostname = entry.get("hostname", "")
                if hostname:
                    new_index[hostname.lower()] = entry
            self._index = new_index
            self._mtime = current_mtime
            return True
        return False

    def get_by_hostname(self, hostname: str) -> dict[str, Any] | None:
        """Look up a registry entry by hostname. Returns None if not found."""
        self._reload_if_stale()
        return self._index.get(hostname.lower())

    def get_by_service(self, user_name: str, service_name: str, label: str, domain: str = "localhost") -> dict[str, Any] | None:
        """Look up by user/service/label. Constructs the hostname from these."""
        hostname = f"{service_name}-{user_name}-{label}.{domain}"
        return self.get_by_hostname(hostname)
