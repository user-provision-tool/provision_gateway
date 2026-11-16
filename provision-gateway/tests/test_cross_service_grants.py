"""G83 — the cross-path verdict, the dnsmasq writer, and the caller trust.

Unit tests with no live gateway: the grant matrix, the "only the cross nginx"
caller rule, and the grant-driven resolver config. The live end-to-end run is
§10 of the task doc.
"""

from __future__ import annotations

import logging
import types

import pytest

from app.services import cross_values, cross_verify
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
    # G91 — the value cache is process-wide; every test starts empty.
    cross_values.reset()
    yield
    cross_values.reset()


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
    monkeypatch.setattr(cross_verify, "cross_nginx_ip", lambda: "172.28.0.2")
    monkeypatch.setattr(cross_verify, "_ptr_name", lambda ip: "some-app")
    assert cross_verify.is_cross_nginx_caller("172.28.0.2")
    assert cross_verify.is_cross_nginx_caller("::ffff:172.28.0.2")
    assert not cross_verify.is_cross_nginx_caller("172.28.0.16")


def test_the_shared_network_caller_is_trusted_by_container_identity(monkeypatch):
    """G85 — the stream hook's fetch can leave over the shared network."""
    monkeypatch.setattr(cross_verify, "cross_nginx_ip", lambda: "172.28.0.2")
    monkeypatch.setattr(cross_verify, "_ptr_name",
                        lambda ip: "subnet-acl-cross-nginx" if ip == "172.17.0.5" else "app")
    assert cross_verify.is_cross_nginx_caller("172.17.0.5")
    assert not cross_verify.is_cross_nginx_caller("172.17.0.9")


def test_cross_values_refresh_reports_each_change(monkeypatch):
    """G91 — one resolve reports which dynamic value moved."""
    names = {}
    monkeypatch.setattr(
        cross_values, "_resolve",
        lambda name: names.get(name, "10.0.0.9"),
    )
    names[settings.CROSS_NGINX_NAME] = "10.0.0.2"
    names[settings.CROSS_RESOLVER_NAME] = "10.0.0.3"
    first = cross_values.refresh()
    assert not first["cross_nginx_changed"] and not first["resolver_changed"]

    names[settings.CROSS_NGINX_NAME] = "10.0.0.4"
    second = cross_values.refresh()
    assert second["cross_nginx_changed"] and not second["resolver_changed"]
    assert second["cross_nginx_ip"] == "10.0.0.4"

    names[settings.CROSS_RESOLVER_NAME] = "10.0.0.5"
    third = cross_values.refresh()
    assert not third["cross_nginx_changed"] and third["resolver_changed"]
    assert third["resolver_ip"] == "10.0.0.5"


def test_cross_values_keep_the_last_value_on_a_failed_resolve(monkeypatch):
    cross_values.set_current(cross_nginx_ip="10.0.0.2", resolver_ip="10.0.0.3")
    monkeypatch.setattr(cross_values, "_resolve", lambda name: None)
    result = cross_values.refresh()
    assert result["cross_nginx_ip"] == "10.0.0.2"
    assert result["resolver_ip"] == "10.0.0.3"
    assert not result["cross_nginx_changed"] and not result["resolver_changed"]


def test_dnsmasq_config_is_grant_driven_and_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "CROSS_CONFIG_DIR", tmp_path)
    cross_values.set_current(cross_nginx_ip="172.28.0.2")
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


def test_stream_verdict_logs_the_raw_source_ip_and_never_the_handle(monkeypatch, caplog):
    """G89 — the stream verdict adds the RAW source IP to the resolved id.

    The handle is a credential, so it must never appear in the log; the raw IP
    (the value the cross nginx sent as X-Source-IP) is the new observable.
    """
    monkeypatch.setattr(cross_verify, "resolve_source", lambda ip: _source())
    monkeypatch.setattr(
        cross_verify, "_stream_target",
        lambda db, registry, handle: (
            types.SimpleNamespace(canonical_name="target--app.example.com", port=9000),
            {"service_id": "target-1"},
            None,
        ),
    )
    monkeypatch.setattr(
        cross_verify, "decide",
        lambda db, source, target: {"allowed": True, "reason": "grant"},
    )
    with caplog.at_level(logging.INFO, logger="gateway.cross_verify"):
        verdict = cross_verify.stream_verdict(
            "172.28.0.9", "secret-handle.example.com", _Db(), None
        )
    assert verdict["allowed"] is True
    assert "source_ip=172.28.0.9" in caplog.text
    assert "source=source-1" in caplog.text
    assert "secret-handle.example.com" not in caplog.text


def test_stream_verdict_denial_logs_the_raw_source_ip(monkeypatch, caplog):
    monkeypatch.setattr(cross_verify, "resolve_source", lambda ip: _source())
    monkeypatch.setattr(
        cross_verify, "_stream_target",
        lambda db, registry, handle: (
            None, None, {"allowed": False, "status": 403, "reason": "unknown handle"}
        ),
    )
    with caplog.at_level(logging.INFO, logger="gateway.cross_verify"):
        verdict = cross_verify.stream_verdict(
            "172.28.0.9", "secret-handle.example.com", _Db(), None
        )
    assert verdict["allowed"] is False
    assert "source_ip=172.28.0.9" in caplog.text
    assert "secret-handle.example.com" not in caplog.text


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


def test_targeted_sync_touches_only_the_affected_source(monkeypatch):
    """G94 R10.1 — a grant click reads/submits its source and no other."""
    import asyncio
    from app.services import cross_orchestrator as co

    orch = CrossOrchestrator()
    calls = []
    entries = {
        "source-1": {"service_id": "source-1", "status": "running",
                     "extra_resolver": None, "extra_network": None},
        "other-1": {"service_id": "other-1", "status": "running",
                    "extra_resolver": "stale", "extra_network": "stale"},
    }
    monkeypatch.setattr(co, "SessionLocal", _FakeSession)
    monkeypatch.setattr(co.cross_values, "current_cross_nginx_ip", lambda: "172.28.0.2")
    monkeypatch.setattr(co.cross_values, "current_resolver_ip", lambda: "172.28.0.3")
    monkeypatch.setattr(orch, "write_dnsmasq_config",
                        lambda domains, cross_nginx_ip=None: False)
    monkeypatch.setattr(co.service_grants, "reachable_domains", lambda db: {"localhost"})
    monkeypatch.setattr(co.service_grants, "entries_by_id", lambda: entries)
    monkeypatch.setattr(
        co.service_grants, "targets_for",
        lambda db, entry: ([{"service_id": "target-1"}]
                           if entry and entry["service_id"] == "source-1" else []))
    monkeypatch.setattr(co.service_grants, "live", lambda entry: True)

    async def fake_apply(service_id, present, *, resolver_ip=None):
        calls.append((service_id, present, resolver_ip))
        return True

    monkeypatch.setattr(orch, "_apply", fake_apply)
    result = asyncio.run(orch.sync_source("source-1"))

    assert result["applied"] == 1
    assert calls == [("source-1", True, "172.28.0.3")]
    # The other stale source is the global reconciler's job, not this click's.
    assert all(service_id != "other-1" for service_id, _p, _r in calls)


def test_global_reconciler_diffs_stored_pairs_without_http_reads(monkeypatch):
    """G94 R10.2/R10.3 — one entries query drives the pass; no per-source GET."""
    import asyncio
    from app.services import cross_orchestrator as co

    orch = CrossOrchestrator()
    entries = {
        "s1": {"service_id": "s1", "status": "running",
               "extra_resolver": "172.28.0.3",
               "extra_network": settings.CROSS_NETWORK},
        "s2": {"service_id": "s2", "status": "running",
               "extra_resolver": "old", "extra_network": "old"},
        "s3": {"service_id": "s3", "status": "running",
               "extra_resolver": "old", "extra_network": "old"},
    }
    applied = []
    monkeypatch.setattr(co, "SessionLocal", _FakeSession)
    monkeypatch.setattr(co.cross_values, "current_cross_nginx_ip", lambda: "172.28.0.2")
    monkeypatch.setattr(co.cross_values, "current_resolver_ip", lambda: "172.28.0.3")
    monkeypatch.setattr(orch, "write_dnsmasq_config",
                        lambda domains, cross_nginx_ip=None: False)
    monkeypatch.setattr(co.service_grants, "reachable_domains", lambda db: {"localhost"})
    monkeypatch.setattr(co.service_grants, "sources_with_targets",
                        lambda db: {"s1": [], "s2": []})
    monkeypatch.setattr(co.service_grants, "entries_by_id", lambda: entries)
    monkeypatch.setattr(co.service_grants, "live", lambda entry: True)

    async def fake_apply(service_id, present, *, resolver_ip=None):
        applied.append((service_id, present))
        return True

    async def _no_http_read(service_id):
        raise AssertionError("the global reconciler must not GET each source")

    monkeypatch.setattr(orch, "_apply", fake_apply)
    monkeypatch.setattr(co.provision_service, "get_extra_resolver", _no_http_read)
    result = asyncio.run(orch.sync_all())

    # s1 already carries the desired pair; s2 is stale; s3 lost its targets.
    assert applied == [("s2", True), ("s3", False)]
    assert result["applied"] == 2
    assert result["cleared"] == 1
    assert result["failed"] == 0
