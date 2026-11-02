"""T6 — import the legacy gateway SQLite store into the ``gateway`` schema.

Run inside the gateway container after the T2 registry import (the pair map is
its input for shared procedure/identity allocation):

    python -m app.tools.import_legacy \
      --sqlite /srv/provision_subnet_acl/gateway_data/gateway.db \
      --pair-map /srv/provision_subnet_acl/generated/registry_import_pair_map.json \
      --report /srv/provision_subnet_acl/generated/gateway_import_report.json

Every step is idempotent (ON CONFLICT / existence checks), credentials are
redacted, imported API keys are revoked, and no live admin remains: the first
admin comes from the local bootstrap command (T6 step 7).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from ..config import settings
from ..database import SessionLocal, engine, init_db
from ..services import keyring
from ..services.api_users_client import ApiUsersClient

NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SECRET_KEYS = {"passwd", "passwd_plain", "password", "privkey", "api_key", "token",
               "resume_values"}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items() if k not in SECRET_KEYS}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _dt(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text_value = str(value)
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text_value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _json(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(_redact(value))
    try:
        return json.dumps(_redact(json.loads(value)))
    except (TypeError, ValueError):
        return None


class Importer:
    def __init__(self, sqlite_path: str, pair_map_path: str | None):
        self.sqlite_path = sqlite_path
        self.conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        self.client = ApiUsersClient()
        self.report: dict[str, Any] = {
            "counts": {}, "skipped": [], "errors": [], "notes": [],
            "dropped_grants": [], "dropped_exposures": [], "email_collisions": [],
        }
        self.end_ids: dict[int, uuid.UUID] = {}
        self.admin_ids: dict[int, uuid.UUID] = {}
        self.api_users: dict[str, uuid.UUID] = {}
        self.job_ids: dict[int, str] = {}
        self.pair = {"procedures": [], "tuples": {}}
        if pair_map_path and Path(pair_map_path).is_file():
            self.pair = json.loads(Path(pair_map_path).read_text(encoding="utf-8"))
        self.proc_by_job = {str(p["job_id"]): p["procedure_id"]
                            for p in self.pair.get("procedures", []) if p.get("job_id")}
        self.proc_by_task = {str(p["task_id"]): p["procedure_id"]
                             for p in self.pair.get("procedures", []) if p.get("task_id")}
        self.tuple_ids = {k: uuid.UUID(v) for k, v in (self.pair.get("tuples") or {}).items()}

    def rows(self, table: str):
        try:
            return [dict(r) for r in self.conn.execute(f"SELECT * FROM {table}")]
        except sqlite3.Error:
            return []

    # -- helpers -----------------------------------------------------------
    def ensure_api_user(self, username: str, deleted: bool = False) -> uuid.UUID | None:
        if username in self.api_users:
            return self.api_users[username]
        existing = db_exec(
            "SELECT id, deleted_at FROM registry.api_users WHERE username=:u",
            {"u": username},
        )
        if existing:
            uid = existing[0]
            if deleted and existing[1] is None:
                try:
                    self.client.soft_delete(str(uid))
                except Exception:
                    pass
            self.api_users[username] = uid
            return uid
        try:
            data = self.client.ensure(username, mode="owner")
            uid = uuid.UUID(str(data["id"]))
            if deleted:
                self.client.soft_delete(str(uid))
            self.api_users[username] = uid
            return uid
        except Exception as exc:  # noqa: BLE001
            self.report["errors"].append(f"api_user {username}: {exc}")
            return None

    def login_taken(self, db, login_value: str) -> bool:
        return db.execute(
            text("SELECT 1 FROM gateway.user_logins WHERE login = :l"),
            {"l": login_value},
        ).first() is not None

    def service_id_for(self, owner: str, service: str, label: str):
        key = f"{owner}|{service}|{label}"
        if key in self.tuple_ids:
            return self.tuple_ids[key]
        row = db_exec(
            "SELECT s.id FROM registry.services s JOIN registry.api_users a "
            "ON a.id=s.owner_api_user_id WHERE a.username=:u AND s.service_name=:s "
            "AND s.label=:l AND s.deleted_at IS NULL",
            {"u": owner, "s": service, "l": str(label)},
        )
        return row[0] if row else None

    # -- steps -------------------------------------------------------------
    def import_accounts(self, db) -> None:
        users = self.rows("end_users")
        admins = self.rows("admins")
        # Pre-scan reserved login strings (other accounts' usernames/emails).
        for u in users:
            username = str(u.get("username") or "")
            extra_email = None
            if not NAME_RE.match(username) or "--" in username:
                if u.get("deleted_at") is not None:
                    # A tombstoned account cannot log in; keep it importable with
                    # a name-safe generated username and report the substitution.
                    username = f"user-{u['id']}"
                    self.report["notes"].append(
                        f"soft-deleted legacy username replaced with {username!r}"
                    )
                elif EMAIL_RE.match(username):
                    # Operator ruling 2026-11-01: an email-named legacy account
                    # is imported under a generated name-safe username, with the
                    # email kept as its optional email login. An email is never
                    # the username.
                    email_login = username.strip().lower()
                    base = f"user-{u['id']}"
                    username = base
                    suffix = 1
                    while self.login_taken(db, username):
                        suffix += 1
                        username = f"{base}-{suffix}"
                    self.report["notes"].append(
                        f"email username {email_login!r} imported as {username!r} "
                        "with the email as an email login"
                    )
                    if self.login_taken(db, email_login):
                        self.report["email_collisions"].append(
                            {"end_user": u["id"], "email": email_login}
                        )
                        email_login = None
                    extra_email = email_login
                else:
                    # A live username that is neither name-safe nor an email is a
                    # cutover blocker.
                    self.report["errors"].append(
                        f"live username violates charset: {u.get('username')!r}"
                    )
                    continue
            api_uid = self.ensure_api_user(username, deleted=u.get("deleted_at") is not None)
            if api_uid is None:
                continue
            role = "special" if u.get("role") == "special" else "viewer"
            uid = uuid.uuid4()
            self.end_ids[int(u["id"])] = uid
            approved = bool(u.get("is_approved"))
            active = bool(u.get("is_active"))
            db.execute(text(
                "INSERT INTO gateway.users (id, api_user_id, role, password_hash, "
                "is_approved, is_active, is_public_shared, deletion_state, deleted_at, "
                "created_at, approved_at, last_login_at) VALUES "
                "(:id,:api,:role,:pw,:ap,:ac,:pub,'none',:del,:cr,:apr,NULL) "
                "ON CONFLICT (api_user_id) DO NOTHING"
            ), {"id": uid, "api": api_uid, "role": role,
                "pw": u.get("password_hash") or "",
                "ap": approved, "ac": active, "pub": bool(u.get("is_public_shared")),
                "del": _dt(u.get("deleted_at")), "cr": _dt(u.get("created_at")),
                "apr": _dt(u.get("approved_at"))})
            uid = db.execute(
                text("SELECT id FROM gateway.users WHERE api_user_id=:a"), {"a": api_uid}
            ).scalar()
            self.end_ids[int(u["id"])] = uid
            if not self.login_taken(db, username):
                db.execute(text(
                    "INSERT INTO gateway.user_logins (user_id, login, kind) "
                    "VALUES (:u,:l,'username') ON CONFLICT (login) DO NOTHING"
                ), {"u": uid, "l": username})
            if extra_email:
                db.execute(text(
                    "INSERT INTO gateway.user_logins (user_id, login, kind) "
                    "VALUES (:u,:l,'email') ON CONFLICT (login) DO NOTHING"
                ), {"u": uid, "l": extra_email})
        # Legacy admins -> viewer + generated username + optional email login.
        for a in admins:
            old_id = int(a["id"])
            base = f"admin-{old_id}"
            username = base
            suffix = 1
            while self.login_taken(db, username):
                suffix += 1
                username = f"{base}-{suffix}"
            api_uid = self.ensure_api_user(username)
            if api_uid is None:
                continue
            uid = uuid.uuid4()
            self.admin_ids[old_id] = uid
            db.execute(text(
                "INSERT INTO gateway.users (id, api_user_id, role, password_hash, "
                "is_approved, is_active, is_public_shared, deletion_state, deleted_at, "
                "created_at, approved_at, last_login_at) VALUES "
                "(:id,:api,'viewer',:pw,true,true,false,'none',NULL,:cr,:cr,:ll) "
                "ON CONFLICT (api_user_id) DO NOTHING"
            ), {"id": uid, "api": api_uid, "pw": a.get("password_hash") or "",
                "cr": _dt(a.get("created_at")), "ll": _dt(a.get("last_login_at"))})
            uid = db.execute(
                text("SELECT id FROM gateway.users WHERE api_user_id=:a"), {"a": api_uid}
            ).scalar()
            self.admin_ids[old_id] = uid
            db.execute(text(
                "INSERT INTO gateway.user_logins (user_id, login, kind) "
                "VALUES (:u,:l,'username') ON CONFLICT (login) DO NOTHING"
            ), {"u": uid, "l": username})
            email = str(a.get("email") or "").strip().lower()
            if email:
                taken = self.login_taken(db, email)
                if taken:
                    self.report["email_collisions"].append({"admin": old_id, "email": email})
                else:
                    db.execute(text(
                        "INSERT INTO gateway.user_logins (user_id, login, kind) "
                        "VALUES (:u,:l,'email') ON CONFLICT (login) DO NOTHING"
                    ), {"u": uid, "l": email})
            self.report["notes"].append(f"legacy admin {old_id} -> viewer {username!r}")
        db.commit()
        self.report["counts"]["users_end"] = len(self.end_ids)
        self.report["counts"]["users_admin"] = len(self.admin_ids)

    def import_keys(self, db) -> None:
        for row in self.rows("api_keys"):
            target_type = row.get("target_type") or "end_user"
            old_uid = int(row["user_id"])
            if target_type == "admin":
                uid = self.admin_ids.get(old_uid)
            else:
                uid = self.end_ids.get(old_uid)
            if uid is None:
                self.report["skipped"].append({"api_key": row.get("id"), "reason": "target unresolved"})
                continue
            db.execute(text(
                "INSERT INTO gateway.api_keys (id, user_id, label, token_hash, mask, "
                "is_default, created_at, expires_at, is_revoked, last_used_at) VALUES "
                "(:id,:u,:label,:hash,:mask,false,:cr,:exp,true,:used) "
                "ON CONFLICT (id) DO NOTHING"
            ), {"id": int(row["id"]), "u": uid, "label": row.get("label") or "imported",
                "hash": row.get("token_hash") or str(uuid.uuid4()), "mask": row.get("mask"),
                "cr": _dt(row.get("created_at")), "exp": _dt(row.get("expires_at")),
                "used": _dt(row.get("last_used_at"))})
        db.execute(text(
            "SELECT setval(pg_get_serial_sequence('gateway.api_keys','id'), "
            "COALESCE((SELECT max(id) FROM gateway.api_keys), 1))"
        ))
        db.commit()

    def import_grants(self, db) -> None:
        for row in self.rows("acl_grants"):
            grantee = self.end_ids.get(int(row["grantee_user_id"]))
            shared = self.end_ids.get(int(row["shared_user_id"]))
            if grantee is None or shared is None:
                self.report["dropped_grants"].append(dict(row))
                continue
            role = db.execute(text("SELECT role FROM gateway.users WHERE id=:id"), {"id": shared}).scalar()
            if role not in ("special", "admin"):
                self.report["dropped_grants"].append(dict(row))
                continue
            db.execute(text(
                "INSERT INTO gateway.acl_grants (grantee_user_id, shared_user_id, state, "
                "created_at, updated_at) VALUES (:g,:s,:st,:cr,:up) "
                "ON CONFLICT (grantee_user_id, shared_user_id) DO NOTHING"
            ), {"g": grantee, "s": shared, "st": row.get("state") or "active",
                "cr": _dt(row.get("created_at")), "up": _dt(row.get("updated_at"))})
        db.commit()

    def import_exposures(self, db) -> None:
        owner_names = {int(r["id"]): r.get("username") for r in self.rows("end_users")}
        for row in self.rows("stream_exposures"):
            owner = self.end_ids.get(int(row["owner_user_id"]))
            sid = self.service_id_for(owner_names.get(int(row["owner_user_id"]), ""),
                                      row.get("service_name"), row.get("label"))
            if owner is None or sid is None:
                self.report["dropped_exposures"].append(dict(row))
                continue
            db.execute(text(
                "INSERT INTO gateway.stream_exposures (handle_hash, owner_user_id, service_id, "
                "sub_service, port, canonical_name, created_at, revoked_at) VALUES "
                "(:h,:o,:s,:sub,:p,:cn,:cr,:rv) ON CONFLICT (handle_hash) DO NOTHING"
            ), {"h": row["handle_hash"], "o": owner, "s": sid,
                "sub": row.get("sub_service"), "p": int(row.get("port") or 0),
                "cn": row.get("canonical_name"), "cr": _dt(row.get("created_at")),
                "rv": _dt(row.get("revoked_at"))})
        db.commit()

    def import_audit(self, db) -> None:
        for row in self.rows("audit_log"):
            actor = self.admin_ids.get(int(row["admin_id"])) if row.get("admin_id") else None
            detail = None
            if row.get("detail_json"):
                try:
                    detail = json.dumps(_redact(json.loads(row["detail_json"])))
                except (TypeError, ValueError):
                    detail = None
            db.execute(text(
                "INSERT INTO gateway.audit_log (id, actor_user_id, action, target_user, "
                "target_service, target_label, detail_json, status, error_message, "
                "ip_address, created_at) VALUES (:id,:a,:act,:tu,:ts,:tl,"
                "CAST(:d AS jsonb),:st,:err,:ip,:cr) ON CONFLICT (id) DO NOTHING"
            ), {"id": int(row["id"]), "a": actor, "act": row.get("action") or "unknown",
                "tu": row.get("target_user"), "ts": row.get("target_service"),
                "tl": row.get("target_label"), "d": detail,
                "st": row.get("status") or "success", "err": row.get("error_message"),
                "ip": row.get("ip_address"), "cr": _dt(row.get("created_at"))})
        db.execute(text(
            "SELECT setval(pg_get_serial_sequence('gateway.audit_log','id'), "
            "COALESCE((SELECT max(id) FROM gateway.audit_log), 1))"
        ))
        db.commit()

    def import_config(self, db) -> None:
        for table in ("gateway_settings", "system_config", "llm_config", "proxy_configs",
                      "service_templates"):
            rows = self.rows(table)
            for row in rows:
                cols = [c for c in row if c not in ("id",)]
                # Only columns that exist in the target table.
                existing = {r[0]: r[1] for r in db.execute(text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema='gateway' AND table_name=:t"
                ), {"t": table})}
                cols = [c for c in cols if c in existing]
                values = {}
                for c in cols:
                    value = row[c]
                    if existing[c] == "boolean" and value in (0, 1):
                        value = bool(value)
                    values[c] = value
                placeholders = ",".join(f":{c}" for c in cols)
                sql = (f"INSERT INTO gateway.{table} ({','.join(cols)}) "
                       f"VALUES ({placeholders}) ON CONFLICT DO NOTHING")
                try:
                    db.execute(text(sql), values)
                except Exception as exc:  # noqa: BLE001
                    self.report["errors"].append(f"config {table}: {exc}")
                    db.rollback()
        db.commit()

    def import_generation_tasks(self, db) -> None:
        for row in self.rows("generation_jobs"):
            old_id = int(row["id"])
            existing = db.execute(
                text("SELECT task_id FROM gateway.tasks WHERE collision_token=:c"),
                {"c": str(old_id)},
            ).scalar()
            if existing:
                self.job_ids[old_id] = existing
                continue
            new_id = f"gt-{uuid.uuid4().hex[:12]}"
            self.job_ids[old_id] = new_id
            request = {}
            try:
                request = json.loads(row.get("request") or "{}")
            except (TypeError, ValueError):
                request = {}
            service_name = row.get("service_name")
            label = str(row.get("target_label") or request.get("label") or "0")
            owner = row.get("target_user") or request.get("user_name")
            sid = self.service_id_for(owner, service_name, label) if owner else None
            status = row.get("status") or "unknown"
            result = None
            if row.get("result"):
                try:
                    result = _redact(json.loads(row["result"]))
                except (TypeError, ValueError):
                    result = None
                if isinstance(result, dict) and isinstance(result.get("files"), dict):
                    result["files"] = {
                        name: {"name": name, "sha256": "", "size": 0}
                        for name in result["files"]
                    }
            if status in ("queued", "running"):
                status = "unknown"
            elif status == "awaiting_input":
                pending = None
                try:
                    pending = json.loads(row.get("pending_input") or "{}")
                except (TypeError, ValueError):
                    pass
                result = {"pause": {"prompt": (pending or {}).get("prompt", ""),
                                    "fields": (pending or {}).get("vars", []),
                                    "request": request, "redacted": True}}
                status = "paused"
            p = self.proc_by_job.get(str(old_id)) or str(uuid.uuid4())
            db.execute(text(
                "INSERT INTO gateway.tasks (task_id, collision_token, procedure_id, "
                "service_id, executor, kind, operation, origin, status, phase, "
                "phase_index, phase_total, progress, error, log_ref, usage, request, "
                "result, target_snapshot, events, pending_input, created_at, updated_at) "
                "VALUES (:tid,:coll,:proc,:sid,'gateway',:kind,:op,:origin,:st,:ph,"
                ":pi,:pt,:prog,:err,:log,CAST(:usage AS jsonb),CAST(:req AS jsonb),"
                "CAST(:res AS jsonb),CAST(:snap AS jsonb),CAST(:events AS jsonb),"
                "CAST(:pending AS jsonb),:cr,:up) ON CONFLICT (task_id) DO NOTHING"
            ), {"tid": new_id, "coll": str(old_id), "proc": uuid.UUID(p), "sid": sid,
                "kind": "analyze" if row.get("job_type") == "fix" else "generate",
                "op": row.get("job_type") or "generate_missing",
                "origin": ("fix" if row.get("job_type") == "fix"
                           else ("deploy" if owner else "standalone")),
                "st": status, "ph": row.get("phase"), "pi": row.get("phase_index"),
                "pt": row.get("phase_total"), "prog": row.get("progress"),
                "err": row.get("error"),
                "log": str(Path(settings.GATEWAY_DATA_DIR) / "task_logs" / f"gt-{old_id}.log"),
                "usage": _json(row.get("usage")), "req": _json(request),
                "res": json.dumps(result) if result is not None else None,
                "snap": json.dumps({"service_name": service_name,
                                    "recipe_path": row.get("recipe_path") or "",
                                    "user_name": owner, "label": label}),
                "events": _json(row.get("events")), "pending": _json(row.get("pending_input")),
                "cr": _dt(row.get("created_at")), "up": _dt(row.get("updated_at"))})
        db.commit()
        self.report["counts"]["generation_tasks"] = len(self.job_ids)

    def import_sessions(self, db) -> None:
        for row in self.rows("pre_deploy_sessions"):
            owner = row.get("user_name") or ""
            sid = self.service_id_for(owner, row.get("service_name"), row.get("label"))
            api_row = db.execute(
                text("SELECT id FROM registry.api_users WHERE username=:u"), {"u": owner}
            ).first()
            if sid is None or api_row is None:
                self.report["skipped"].append({"session": row.get("key"), "reason": "tuple unresolved"})
                continue
            state = row.get("state") or "generating"
            if state == "awaiting_input":
                state = "paused"
            # A deployed service keeps its session only as a failed fix card.
            inst = db.execute(
                text("SELECT status FROM registry.service_instances WHERE service_id=:s"),
                {"s": sid},
            ).first()
            if inst is not None and not (inst[0] == "failed" and state in
                                         ("paused", "refused", "generated")):
                self.report["skipped"].append({"session": row.get("key"),
                                               "reason": "deployed service"})
                continue
            proc = (self.proc_by_job.get(str(row.get("job_id")))
                    or self.proc_by_task.get(str(row.get("task_id")))
                    or str(uuid.uuid4()))
            gen_task = self.job_ids.get(int(row["job_id"])) if row.get("job_id") else None
            options = None
            options_secret = None
            try:
                raw_options = json.loads(row.get("options") or "{}")
            except (TypeError, ValueError):
                raw_options = {}
            if isinstance(raw_options, dict) and raw_options:
                secrets = {k: v for k, v in raw_options.items()
                           if k in ("passwd", "password", "passwd_plain")}
                options = _redact(raw_options)
                if secrets:
                    try:
                        options_secret = keyring.encrypt(json.dumps(secrets))
                    except Exception:
                        options_secret = None
            db.execute(text(
                "INSERT INTO gateway.pre_deploy_sessions (service_id, procedure_id, "
                "owner_api_user_id, service_name, service_label, recipe_path, state, "
                "reason, detail, options, options_secret_enc, options_key_version, "
                "generation_task_id, deploy_task_id, guide, slot, use_global_proxy, "
                "created_at, updated_at) VALUES (:sid,:proc,:owner,:svc,:label,:recipe,"
                ":state,:reason,:detail,CAST(:opts AS jsonb),:secret,1,:gen,:deploy,"
                ":guide,:slot,:proxy,:cr,:up) "
                "ON CONFLICT (service_id) DO NOTHING"
            ), {"sid": sid, "proc": uuid.UUID(proc), "owner": api_row[0],
                "svc": row.get("service_name"), "label": str(row.get("label") or "0"),
                "recipe": row.get("recipe_path") or "", "state": state,
                "reason": row.get("reason"), "detail": row.get("detail"),
                "opts": json.dumps(options) if options is not None else None,
                "secret": options_secret, "gen": gen_task,
                "deploy": row.get("task_id") or None,
                "guide": row.get("guide"), "slot": row.get("slot"),
                "proxy": bool(row.get("use_global_proxy")),
                "cr": _dt(row.get("created_at")), "up": _dt(row.get("updated_at"))})
        db.commit()

    def run(self) -> dict:
        init_db()
        db = SessionLocal()
        try:
            self.import_accounts(db)
            self.import_keys(db)
            self.import_grants(db)
            self.import_exposures(db)
            self.import_audit(db)
            self.import_config(db)
            self.import_generation_tasks(db)
            self.import_sessions(db)
            # First-admin gate: registration stays closed until bootstrap.
            db.execute(text(
                "INSERT INTO gateway.gateway_settings (key, value) VALUES "
                "('registration_open','false') ON CONFLICT (key) DO UPDATE SET value='false'"
            ))
            db.execute(text(
                "INSERT INTO gateway.gateway_settings (key, value) VALUES ('token_epoch','1') "
                "ON CONFLICT (key) DO UPDATE SET value='1'"
            ))
            db.execute(text(
                "INSERT INTO gateway.import_state (stage, detail) VALUES ('sqlite_gateway', "
                "CAST(:d AS jsonb)) ON CONFLICT (stage) DO UPDATE SET completed_at=now(), "
                "detail=EXCLUDED.detail"
            ), {"d": json.dumps(self.report["counts"])})
            db.commit()
            for table in ("users", "user_logins", "api_keys", "acl_grants",
                          "stream_exposures", "audit_log", "tasks", "pre_deploy_sessions"):
                self.report["counts"][table] = db.execute(
                    text(f"SELECT count(*) FROM gateway.{table}")
                ).scalar()
        finally:
            db.close()
        return self.report


def db_exec(sql: str, params: dict):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).first()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", required=True)
    ap.add_argument("--pair-map", default=None)
    ap.add_argument("--report", default=None)
    args = ap.parse_args(argv)
    imp = Importer(args.sqlite, args.pair_map)
    report = imp.run()
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("counts", "errors", "skipped")}, indent=2))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
