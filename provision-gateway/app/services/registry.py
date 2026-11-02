"""Read-only registry access — on demand, as gateway_reader (T9/R1.3).

No YAML file, no mtime cache, no version row: every call reads committed
PostgreSQL state through the read-only role. The gateway may read registry
tables; it never writes them and never calls backwards into the -api for a
registry read.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from ..database import reader_engine

_BASE = """
SELECT s.id                AS service_id,
       s.owner_api_user_id,
       a.username          AS user_name,
       a.deleted_at        AS owner_deleted_at,
       s.service_name,
       s.label,
       s.recipe_path,
       s.deleted_at        AS service_deleted_at,
       i.status,
       i.hostname,
       i.subnet,
       i.gateway,
       i.network_name,
       i.compose_template_path,
       i.compose_file_path,
       i.nginx_conf_template_path,
       i.nginx_conf_path,
       i.nginx_conf_paths,
       i.nginx_conf_paths_by_category,
       i.htpasswd_path,
       i.ssl_certificate_path,
       i.ssl_certificate_key_path,
       i.env_file_path,
       i.env_files,
       i.container_names,
       i.profiles,
       i.volumes,
       i.build_args,
       i.tls
  FROM registry.services s
  JOIN registry.api_users a ON a.id = s.owner_api_user_id
  LEFT JOIN registry.service_instances i ON i.service_id = s.id
 WHERE s.deleted_at IS NULL
"""


def _endpoints(service_ids: list[str]) -> dict[str, list[dict]]:
    if not service_ids:
        return {}
    with reader_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT service_id, served_name, kind, sub_service, port "
                "FROM registry.service_endpoints WHERE service_id = ANY(:ids) ORDER BY id"
            ),
            {"ids": service_ids},
        ).mappings().all()
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(str(row["service_id"]), []).append(dict(row))
    return out


def _entry(row: dict, endpoints: list[dict]) -> dict[str, Any]:
    entry = dict(row)
    entry["service_id"] = str(row["service_id"])
    entry["owner_api_user_id"] = str(row["owner_api_user_id"])
    entry["label"] = str(row["label"])
    entry["http_endpoints"] = [e["served_name"] for e in endpoints if e["kind"] == "http"]
    entry["stream_sub_services"] = [e["served_name"] for e in endpoints if e["kind"] == "stream"]
    entry["stream_rows"] = [e for e in endpoints if e["kind"] == "stream"]
    entry["recipe_path"] = row.get("recipe_path") or ""
    entry["tls"] = bool(row.get("tls"))
    return entry


def _rows(sql: str, params: dict | None = None) -> list[dict]:
    with reader_engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params or {}).mappings().all()]


class Registry:
    """Compatibility façade over the read-on-demand registry."""

    def __init__(self, registry_path: str | None = None) -> None:
        # Kept for the constructor call sites; the path is no longer a store.
        self._path = registry_path

    def get_all_entries(self) -> list[dict[str, Any]]:
        rows = _rows(_BASE + " ORDER BY a.username, s.service_name, s.label")
        emap = _endpoints([r["service_id"] for r in rows])
        return [_entry(r, emap.get(str(r["service_id"]), [])) for r in rows]

    def get_entry(self, user_name: str, service_name: str, label: str) -> dict[str, Any] | None:
        rows = _rows(
            _BASE + " AND a.username=:u AND s.service_name=:s AND s.label=:l",
            {"u": user_name, "s": service_name, "l": str(label)},
        )
        if not rows:
            return None
        emap = _endpoints([rows[0]["service_id"]])
        return _entry(rows[0], emap.get(str(rows[0]["service_id"]), []))

    def get_by_id(self, service_id: str) -> dict[str, Any] | None:
        rows = _rows(_BASE + " AND s.id=:id", {"id": service_id})
        if not rows:
            return None
        emap = _endpoints([rows[0]["service_id"]])
        return _entry(rows[0], emap.get(str(rows[0]["service_id"]), []))
