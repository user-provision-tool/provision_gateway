"""The one gateway-vs-edge ACL agreement rule (R11).

Moved out of ``routers/system.py`` so the status page and the ACL-drift condition
source share exactly one implementation. The rule is unchanged.
"""

from __future__ import annotations

from ..config import settings


def acl_state(edge_env: dict[str, str] | None) -> dict:
    """The gateway-vs-edge ACL state.

    G80 — fullset is ACL-on unconditionally and the ``ENABLE_ACL`` switch is
    gone from the deployment (the edge template renders the gate by default).
    An ABSENT value therefore means ACL-on; a PRESENT ``false`` still wins, so
    a deployment that re-adds the switch keeps the drift check that exists
    because of the 2026-08-28 gateway-on/edge-off outage. ``None`` (the edge env
    unreadable) is unknown, never a fabricated mismatch of true-vs-false.
    """
    gateway = bool(settings.ENABLE_ACL)
    if edge_env is None:
        edge = None
    else:
        edge = edge_env.get("ENABLE_ACL", "true").lower() == "true"
    return {"gateway": gateway, "edge": edge,
            "enabled": bool(gateway and edge),
            "consistent": edge is not None and gateway == edge}
