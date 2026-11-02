"""Test helpers for the PostgreSQL-backed gateway suite.

One user id space (T6): a gateway ``users`` row always has an ``api_users`` row
under the same username. A service row lives in ``registry`` and is owned by
that ``api_users`` row. Helpers write through the ORM/session the application
uses, so tests exercise the real schema and constraints.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import create_engine, text as sqltext

from app.models.user import User
from app.models.user_login import UserLogin


_registry_engine = None


def registry_engine():
    """A ``registry_app`` connection: the -api owns and writes that schema.

    The gateway (``gateway_app``) only has SELECT/REFERENCES on registry tables
    by design, so test setup that seeds owners/services must use the owner role.
    """
    global _registry_engine
    if _registry_engine is None:
        url = os.environ["DATABASE_URL"].replace(
            "gateway_app:gateway_app_pw", "registry_app:registry_app_pw"
        )
        _registry_engine = create_engine(url, pool_pre_ping=True)
    return _registry_engine


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def mk_user(db, username: str = "alice", role: str = "viewer", *,
            approved: bool = True, active: bool = True,
            public: bool = False, password_hash: str = "x",
            deletion_state: str = "none"):
    """Create the api_users row + the gateway users row + the username login."""
    api_id = uuid.uuid4()
    with registry_engine().begin() as conn:
        conn.execute(
            sqltext(
                "INSERT INTO registry.api_users (id, username, state, created_at) "
                "VALUES (:id, :username, 'live', :created_at)"
            ),
            {"id": api_id, "username": username, "created_at": utcnow()},
        )
    user_id = uuid.uuid4()
    db.add(User(
        id=user_id,
        api_user_id=api_id,
        role=role,
        password_hash=password_hash,
        is_approved=approved,
        is_active=active,
        is_public_shared=public,
        deletion_state=deletion_state,
        created_at=utcnow(),
    ))
    db.add(UserLogin(user_id=user_id, login=username, kind="username"))
    db.commit()
    return db.query(User).filter(User.id == user_id).one()


def mk_service(db, owner: User, service_name: str = "myapp", label: str = "0",
               recipe_path: str = ".", service_id=None) -> uuid.UUID:
    """A live service owned by ``owner`` (no instance projection yet)."""
    service_id = service_id or uuid.uuid4()
    with registry_engine().begin() as conn:
        conn.execute(
            sqltext(
                "INSERT INTO registry.services "
                "(id, owner_api_user_id, service_name, label, recipe_path, created_at) "
                "VALUES (:id, :owner, :name, :label, :recipe, :created_at)"
            ),
            {"id": service_id, "owner": owner.api_user_id, "name": service_name,
             "label": str(label), "recipe": recipe_path, "created_at": utcnow()},
        )
    return service_id


def mk_service_instance(db, service_id, *, status: str = "running",
                        **fields: Any) -> None:
    """Insert the 1:1 deployed projection row."""
    from psycopg.types.json import Jsonb

    jsonb_fields = {"failure", "previous_failure", "nginx_conf_paths",
                    "nginx_conf_paths_by_category", "env_files", "volumes",
                    "build_args"}

    cols = ["service_id", "status", "created_at", "updated_at"]
    vals: list[Any] = [str(service_id), status, utcnow(), utcnow()]
    for key, value in fields.items():
        cols.append(key)
        vals.append(Jsonb(value) if key in jsonb_fields else value)
    placeholders = ", ".join(f":p{i}" for i in range(len(vals)))
    params = {f"p{i}": v for i, v in enumerate(vals)}
    with registry_engine().begin() as conn:
        conn.execute(
            sqltext(
                f"INSERT INTO registry.service_instances ({', '.join(cols)}) "
                f"VALUES ({placeholders})"
            ),
            params,
        )


def mk_task(db, task_id: str = "gt-test", *, status: str = "queued",
            procedure_id=None, service_id=None, kind: str = "generate",
            operation: str = "generate_missing", origin: str = "deploy") -> str:
    """A gateway.tasks row (the gateway's own task family)."""
    procedure_id = procedure_id or uuid.uuid4()
    db.execute(
        sqltext(
            "INSERT INTO gateway.tasks (task_id, collision_token, procedure_id, "
            "service_id, executor, kind, operation, origin, status, created_at, updated_at) "
            "VALUES (:tid, :ct, :pid, :sid, 'gateway', :kind, :op, :origin, :status, :now, :now)"
        ),
        {"tid": task_id, "ct": uuid.uuid4().hex[:12], "pid": procedure_id,
         "sid": service_id, "kind": kind, "op": operation, "origin": origin,
         "status": status, "now": utcnow()},
    )
    db.commit()
    return task_id
