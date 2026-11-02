"""Hostname lookup by ``(served_name, kind)`` — on demand, no cache (T9)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from ..database import reader_engine
from .registry import _entry


class HostnameIndex:
    def __init__(self, registry_path: str | None = None) -> None:
        self._registry_path = registry_path  # compatibility; no file is read

    def get_by_hostname(self, hostname: str) -> dict[str, Any] | None:
        if not hostname:
            return None
        with reader_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT s.id AS service_id, s.owner_api_user_id, a.username AS user_name, "
                    "       a.deleted_at AS owner_deleted_at, s.service_name, s.label, "
                    "       s.recipe_path, s.deleted_at AS service_deleted_at, "
                    "       i.status, i.hostname, i.subnet, i.gateway, i.network_name, "
                    "       i.compose_template_path, i.compose_file_path, "
                    "       i.nginx_conf_template_path, i.nginx_conf_path, "
                    "       i.nginx_conf_paths, i.nginx_conf_paths_by_category, "
                    "       i.htpasswd_path, i.ssl_certificate_path, i.ssl_certificate_key_path, "
                    "       i.env_file_path, i.env_files, i.container_names, i.profiles, "
                    "       i.volumes, i.build_args, i.tls "
                    "  FROM registry.service_endpoints e "
                    "  JOIN registry.services s ON s.id = e.service_id "
                    "  JOIN registry.api_users a ON a.id = s.owner_api_user_id "
                    "  LEFT JOIN registry.service_instances i ON i.service_id = s.id "
                    " WHERE lower(e.served_name) = lower(:host) "
                    "   AND e.kind = 'http' AND s.deleted_at IS NULL"
                ),
                {"host": hostname},
            ).mappings().first()
            if not row:
                return None
            eps = conn.execute(
                text(
                    "SELECT service_id, served_name, kind, sub_service, port "
                    "FROM registry.service_endpoints WHERE service_id=:id ORDER BY id"
                ),
                {"id": row["service_id"]},
            ).mappings().all()
        return _entry(dict(row), [dict(e) for e in eps])

    def get_by_service(self, user_name: str, service_name: str, label: str,
                       domain: str = "localhost") -> dict[str, Any] | None:
        from .registry import Registry
        return Registry().get_entry(user_name, service_name, str(label))
