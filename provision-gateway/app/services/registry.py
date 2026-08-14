"""Registry wrapper — read-only access to the provision-api registry YAML.

Gateway mounts the same filesystem as provision-api, so it can read
user_registry.yml directly without HTTP calls. This enables:
- Hostname resolution for /go/{hostname}
- Subnet pool statistics (from the registry entries)
- ACL checks (allowed_special_users per user)
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any


class Registry:
    """Read-only registry wrapper with file-change detection.

    On each read, the file is re-loaded if its mtime has changed.
    Thread-safe via a lock.
    """

    def __init__(self, registry_path: str) -> None:
        self._path = Path(registry_path)
        self._lock = threading.Lock()
        self._entries: list[dict[str, Any]] = []
        self._mtime: float = 0.0

    def _reload_if_stale(self) -> None:
        """Reload from disk if the YAML has changed."""
        if not self._path.exists():
            return
        current_mtime = self._path.stat().st_mtime
        if current_mtime <= self._mtime:
            return
        with self._lock:
            if current_mtime <= self._mtime:
                return
            try:
                import yaml as _yaml
                with open(self._path) as f:
                    data = _yaml.safe_load(f)
                self._entries = data if isinstance(data, list) else []
            except Exception:
                pass
            self._mtime = current_mtime

    def get_all_entries(self) -> list[dict[str, Any]]:
        """Return all registry entries."""
        self._reload_if_stale()
        return list(self._entries)

    def get_entry(self, user_name: str, service_name: str, label: str) -> dict[str, Any] | None:
        """Get a single registry entry by user/service/label."""
        self._reload_if_stale()
        for entry in self._entries:
            if (
                entry.get("user_name") == user_name
                and entry.get("service_name") == service_name
                and str(entry.get("label", "")) == str(label)
            ):
                return entry
        return None
