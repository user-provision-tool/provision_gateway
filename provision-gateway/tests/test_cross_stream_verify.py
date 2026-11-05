"""G85 — the cross stream verdict (`cross-stream-verify`).

Unit tests with no live gateway: the exposure lookup, the instance liveness
(including the paused-conf case), and the SAME service-grant matrix as the HTTP
path (G84). The edge's handle/user ACL is not consulted.
"""

from __future__ import annotations

import types

import pytest

from app.services import cross_verify


class _Query:
    def __init__(self, row):
        self._row = row

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._row


class _Db:
    def __init__(self, row=None):
        self._row = row

    def query(self, model):
        return _Query(self._row if model.__name__ == "StreamExposure" else None)


class _Registry:
    def __init__(self, entry):
        self.entry = entry

    def get_by_id(self, sid):
        return self.entry


def _source(owner="owner-a", sid="source-1"):
    return {"service_id": sid, "owner_api_user_id": owner, "status": "running",
            "service_name": "source", "label": "0", "hostname": "source.localhost"}


def _entry(owner="owner-b", sid="target-1", status="running", **extra):
    entry = {"service_id": sid, "owner_api_user_id": owner, "status": status,
             "service_name": "target", "label": "0", "hostname": "target.localhost",
             "service_deleted_at": None}
    entry.update(extra)
    return entry


def _row(sid="target-1", canonical="target--neo4j.localhost", port=7687):
    return types.SimpleNamespace(
        service_id=sid, canonical_name=canonical, port=port, revoked_at=None)


@pytest.fixture(autouse=True)
def _fixtures(monkeypatch):
    monkeypatch.setattr(cross_verify, "resolve_source", lambda ip: _source())
    monkeypatch.setattr(cross_verify, "_gateway_user",
                        lambda db, api_user_id: types.SimpleNamespace(
                            id="target-user", account_live=True))
    monkeypatch.setattr(cross_verify.acl_service, "is_public_owner",
                        lambda user: False)
    monkeypatch.setattr(cross_verify.acl_service, "allows",
                        lambda db, grantee, shared_id: False)


def _stream(db, entry=None, handle="handle.localhost"):
    return cross_verify.stream_verdict(
        "172.28.0.21", handle, db, _Registry(entry or _entry()))


def test_same_owner_is_allowed_and_returns_the_hop_fields(monkeypatch):
    monkeypatch.setattr(cross_verify, "_stream_target",
                        lambda db, registry, handle: (_row(), _entry(owner="owner-a"), None))
    result = _stream(_Db())
    assert result["allowed"] and result["reason"] == "same_owner"
    assert result["canonical_name"] == "target--neo4j.localhost"
    assert result["port"] == 7687
    assert result["inner_port"] == 7687


def test_public_target_with_a_row_is_allowed_without_an_account_grant(monkeypatch, tmp_path):
    db = _Db(row=_row())
    monkeypatch.setattr(cross_verify, "_gateway_user",
                        lambda db, api_user_id: types.SimpleNamespace(
                            id="target-user", account_live=True))
    monkeypatch.setattr(cross_verify.acl_service, "is_public_owner",
                        lambda user: True)

    class _GrantDb(_Db):
        def query(self, model):
            from app.models.service_grant import ServiceGrant
            if model is ServiceGrant:
                return _Query(object())
            return super().query(model)

    result = cross_verify.stream_verdict(
        "172.28.0.21", "handle.localhost", _GrantDb(row=_row()), _Registry(_entry()))
    assert result["allowed"] and result["reason"] == "grant"


def test_public_target_without_a_row_is_denied(monkeypatch):
    monkeypatch.setattr(cross_verify.acl_service, "is_public_owner",
                        lambda user: True)

    class _GrantDb(_Db):
        def query(self, model):
            from app.models.service_grant import ServiceGrant
            if model is ServiceGrant:
                return _Query(None)
            return super().query(model)

    result = cross_verify.stream_verdict(
        "172.28.0.21", "handle.localhost", _GrantDb(row=_row()), _Registry(_entry()))
    assert not result["allowed"] and "no service grant" in result["reason"]


def test_unknown_source_is_denied(monkeypatch):
    monkeypatch.setattr(cross_verify, "resolve_source", lambda ip: None)
    result = _stream(_Db(row=_row()))
    assert not result["allowed"] and result["status"] == 401


def test_unknown_handle_is_denied():
    result = _stream(_Db(row=None))
    assert not result["allowed"] and result["reason"] == "unknown handle"


def test_revoked_handle_is_denied():
    row = _row()
    row.revoked_at = object()
    result = _stream(_Db(row=row))
    assert not result["allowed"] and result["reason"] == "revoked handle"


def test_dead_instance_is_denied():
    result = _stream(_Db(row=_row()), entry=_entry(status="failed"))
    assert not result["allowed"] and result["reason"] == "instance not running"


def test_paused_stream_conf_is_denied(tmp_path):
    active = tmp_path / "svc.stream.conf"
    paused = tmp_path / ("svc.stream.conf.paused")
    paused.write_text("server {}\n")
    entry = _entry(nginx_conf_paths_by_category={"stream": str(active)})
    result = _stream(_Db(row=_row()), entry=entry)
    assert not result["allowed"] and result["reason"] == "upstream stopped"


def test_private_row_without_an_account_grant_is_denied():
    class _GrantDb(_Db):
        def query(self, model):
            from app.models.service_grant import ServiceGrant
            if model is ServiceGrant:
                return _Query(object())
            return super().query(model)

    result = cross_verify.stream_verdict(
        "172.28.0.21", "handle.localhost", _GrantDb(row=_row()), _Registry(_entry()))
    assert not result["allowed"] and "no active account grant" in result["reason"]
