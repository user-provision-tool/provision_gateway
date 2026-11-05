"""G83 — the cross-path verdict, the dnsmasq writer, and the caller trust.

Unit tests with no live gateway: the grant matrix, the "only the cross nginx"
caller rule, and the grant-driven resolver config. The live end-to-end run is
§10 of the task doc.
"""

from __future__ import annotations

import types

import pytest

from app.services import cross_verify
from app.services.cross_orchestrator import CrossOrchestrator
from app.config import settings


class _Query:
    def __init__(self, row):
        self._row = row

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._row


class _Db:
    def __init__(self, grant_row=None):
        self.grant_row = grant_row

    def query(self, model):
        return _Query(self.grant_row if model.__name__ == "ServiceGrant" else None)


def _source(owner="owner-a", sid="source-1"):
    return {"service_id": sid, "owner_api_user_id": owner, "status": "running",
            "service_name": "source", "label": "0", "hostname": "source.localhost"}


def _target(owner="owner-b", sid="target-1", status="running"):
    return {"service_id": sid, "owner_api_user_id": owner, "status": status,
            "service_name": "target", "label": "0", "hostname": "target.localhost"}


@pytest.fixture(autouse=True)
def _no_account_service(monkeypatch):
    # Every test starts from "a live owner account exists on both sides".
    monkeypatch.setattr(cross_verify, "_gateway_user",
                        lambda db, api_user_id: types.SimpleNamespace(
                            id="target-user", account_live=True))
    monkeypatch.setattr(cross_verify.acl_service, "is_public_owner",
                        lambda user: False)
    monkeypatch.setattr(cross_verify.acl_service, "allows",
                        lambda db, grantee, shared_id: False)


def test_same_owner_is_allowed():
    result = cross_verify.decide(_Db(), _source("o"), _target("o"))
    assert result["allowed"] and result["reason"] == "same_owner"


def test_deactivated_source_owner_is_denied_even_for_same_owner(monkeypatch):
    """G83 fail-closed: a deactivated owner's services are not a source (C12)."""
    monkeypatch.setattr(
        cross_verify, "_gateway_user",
        lambda db, api_user_id: types.SimpleNamespace(
            id="source-user", account_live=False),
    )
    result = cross_verify.decide(_Db(), _source("o"), _target("o"))
    assert not result["allowed"] and result["status"] == 403
    assert "source owner account is not active" in result["reason"]


def test_deactivated_source_owner_is_denied_for_a_public_target(monkeypatch):
    monkeypatch.setattr(
        cross_verify, "_gateway_user",
        lambda db, api_user_id: types.SimpleNamespace(
            id="source-user", account_live=False),
    )
    monkeypatch.setattr(cross_verify.acl_service, "is_public_owner",
                        lambda user: True)
    result = cross_verify.decide(_Db(), _source(), _target())
    assert not result["allowed"] and result["status"] == 403


def test_source_owner_without_a_gateway_account_is_denied(monkeypatch):
    monkeypatch.setattr(cross_verify, "_gateway_user", lambda db, api_user_id: None)
    result = cross_verify.decide(_Db(), _source("o"), _target("o"))
    assert not result["allowed"] and result["status"] == 403


def test_public_target_without_a_row_is_denied(monkeypatch):
    """G84 — public is not an implicit allow; the explicit row is required."""
    monkeypatch.setattr(cross_verify.acl_service, "is_public_owner",
                        lambda user: True)
    result = cross_verify.decide(_Db(), _source(), _target())
    assert not result["allowed"] and result["status"] == 403
    assert "no service grant" in result["reason"]


def test_public_target_with_a_row_needs_no_account_grant(monkeypatch):
    """G84 — the account prerequisite is waived for a live public target."""
    monkeypatch.setattr(cross_verify.acl_service, "is_public_owner",
                        lambda user: True)
    result = cross_verify.decide(_Db(grant_row=object()), _source(), _target())
    assert result["allowed"] and result["reason"] == "grant"


def test_public_target_row_while_owner_deactivated_is_denied():
    """G84 — the waiver is live: a deactivated owner falls back to the account check."""
    result = cross_verify.decide(_Db(grant_row=object()), _source(), _target())
    assert not result["allowed"] and result["status"] == 403


def test_explicit_grant_plus_account_grant_is_allowed(monkeypatch):
    monkeypatch.setattr(cross_verify.acl_service, "allows",
                        lambda db, grantee, shared_id: True)
    result = cross_verify.decide(_Db(grant_row=object()), _source(), _target())
    assert result["allowed"] and result["reason"] == "grant"


def test_explicit_grant_without_account_grant_is_denied():
    result = cross_verify.decide(_Db(grant_row=object()), _source(), _target())
    assert not result["allowed"] and result["status"] == 403


def test_no_grant_is_denied():
    result = cross_verify.decide(_Db(), _source(), _target())
    assert not result["allowed"] and result["status"] == 403
    assert "no service grant" in result["reason"]


def test_dead_target_is_denied():
    result = cross_verify.decide(_Db(), _source(), _target(status="failed"))
    assert not result["allowed"] and "not live" in result["reason"]


def test_unknown_source_is_401():
    result = cross_verify.decide(_Db(), None, _target())
    assert not result["allowed"] and result["status"] == 401


def test_only_the_cross_nginx_ip_is_trusted(monkeypatch):
    monkeypatch.setattr(settings, "CROSS_NGINX_IP", "172.28.0.2")
    monkeypatch.setattr(cross_verify, "_ptr_name", lambda ip: "some-app")
    assert cross_verify.is_cross_nginx_caller("172.28.0.2")
    assert cross_verify.is_cross_nginx_caller("::ffff:172.28.0.2")
    assert not cross_verify.is_cross_nginx_caller("172.28.0.16")


def test_the_shared_network_caller_is_trusted_by_container_identity(monkeypatch):
    """G85 — the stream hook's fetch can leave over the shared network."""
    monkeypatch.setattr(settings, "CROSS_NGINX_IP", "172.28.0.2")
    monkeypatch.setattr(cross_verify, "_ptr_name",
                        lambda ip: "subnet-acl-cross-nginx" if ip == "172.17.0.5" else "app")
    assert cross_verify.is_cross_nginx_caller("172.17.0.5")
    assert not cross_verify.is_cross_nginx_caller("172.17.0.9")


def test_dnsmasq_config_is_grant_driven_and_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "CROSS_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(settings, "CROSS_NGINX_IP", "172.28.0.2")
    o = CrossOrchestrator()
    assert o.write_dnsmasq_config({"localhost", "snaprovision.com"}) is True
    content = (tmp_path / "dnsmasq.d" / "cross.conf").read_text()
    assert "address=/*.localhost/172.28.0.2" in content
    assert "address=/*.snaprovision.com/172.28.0.2" in content
    assert "local=/localhost/" in content and "local=/snaprovision.com/" in content
    assert "no-resolv" in content and "no-hosts" in content
    # Unchanged content is not rewritten (the resolver is not restarted).
    assert o.write_dnsmasq_config({"localhost", "snaprovision.com"}) is False
    # Removing the last reachable target removes its wildcard.
    assert o.write_dnsmasq_config({"localhost"}) is True
    content = (tmp_path / "dnsmasq.d" / "cross.conf").read_text()
    assert "snaprovision.com" not in content
