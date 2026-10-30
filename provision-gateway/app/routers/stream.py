"""Stream router — /api/stream/* (the exposure handles and the per-connection verdict).

Two audiences, two shapes:

* ``/api/stream/exposures`` is the **dashboard's** surface — mint, list, revoke —
  and is authenticated like every other dashboard route (``require_gateway_token``).
  Minting is owner-only, or an admin acting on the owner's behalf (R2.2).
* ``/api/stream/verify`` is the **edge's** surface: the njs hook in the stream
  context POSTs the SNI's handle and gets the verdict back. It carries no auth
  dependency for the same reason ``/api/auth/verify`` does not — the *handle* is
  the credential, and the edge has nothing else to offer. It is reachable only
  from the shared network (the gateway publishes a host port on 127.0.0.1 only).

Task-doc contracts: **C1** ``POST /api/stream/verify {handle} → {allow|deny,
canonical name}`` · **C3** ``POST /api/stream/exposures → {url, once}``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..middleware import require_gateway_token
from ..models.end_user import EndUser
from ..services import stream_service

router = APIRouter(prefix="/api/stream", tags=["stream"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class MintRequest(BaseModel):
    """The row to expose, as the card knows it."""

    service_name: str = Field(..., min_length=1)
    user_name: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)
    sub_service: str = Field(..., min_length=1)
    # Optional on purpose: the registry records NAMES, not ports (§9's ruling),
    # so the card cannot know the port — it is resolved from the instance's own
    # serving_ports artifact below. A caller that DOES send one must agree with
    # the artifact; a disagreement is refused rather than silently preferred.
    port: int | None = Field(None, ge=1, le=65535)


class VerifyRequest(BaseModel):
    """What the edge sends: the handle it read from the client's SNI.

    ``sub_service`` / ``port`` are optional and are **not** sent by the hook — a
    ClientHello names only the handle. A caller that does name a row gets the
    scope check enforced against it (R3.2).
    """

    # Deliberately NOT `min_length=1`: an empty handle is a DENY, not a 422. The
    # contract is "always a verdict" (C1) and the hook's rule is "deny on any
    # non-200" — a validation error would be a second, differently-shaped refusal
    # for the same situation, and `stream_service.verify` already answers it.
    handle: str = ""
    sub_service: str | None = None
    port: int | None = Field(None, ge=1, le=65535)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _instance_entry(request: Request, service_name: str, user_name: str, label: str) -> dict:
    """The registry entry for an instance, or 404 — the -api's record is the truth."""
    registry = getattr(request.app.state, "registry", None)
    entry = registry.get_entry(user_name, service_name, str(label)) if registry else None
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail=f"No such instance: {service_name}/{user_name}/{label}",
        )
    return entry


def _selected_serving_ports(service_name: str) -> list[Path]:
    """The serving-ports artifact(s) the recipe's stored file set SELECTS.

    R2-F12 — the docstring below has always promised "the selection first", and
    the code never read it: with two artifacts in the recipe dir the mint resolved
    against the NEWEST one, whichever the deploy had actually used. Measured
    2026-10-10 on graphiti-local: the deploy carried `serving_ports.base.359.yml`
    (five rows, neo4j's Bolt 7687 among them) while a later LLM generation had
    left `serving_ports.base.360.yml` beside it (no neo4j rows at all) — so the
    mint answered "no such row" for a row the DEPLOYED artifact declares. The
    recorded selection is what the deploy used, so it is what to read.
    """
    from ..services.file_sets import ROOT_RECIPE_KEY, list_file_sets

    project_dir = Path(settings.SOURCE_PROJECTS_DIR) / service_name
    out: list[Path] = []
    try:
        data = list_file_sets(service_name)
    except Exception:  # noqa: BLE001 — a missing state file must not break the mint
        return out
    for recipe_key, entry in (data.get("file_sets") or {}).items():
        name = (entry or {}).get("serving_ports")
        if not name:
            continue
        recipe_dir = (
            project_dir
            if recipe_key in ("", ".", ROOT_RECIPE_KEY)
            else project_dir / recipe_key
        )
        candidate = recipe_dir / name
        if candidate.is_file():
            out.append(candidate)
    return out


def _artifact_kind(service_name: str, sub_service: str) -> str | None:
    """Informational only: the row's KIND from the recipe's artifacts.

    The rendered conf is the authority for membership and port; it does not
    carry a protocol name. The kind is looked up only AFTER membership passes,
    so a filtered-out row is never resurrected by this scan.
    """
    import yaml

    from ..services.stream_conf import row_for_sub_service, stream_rows

    recipe = Path(settings.SOURCE_PROJECTS_DIR) / service_name
    if not recipe.is_dir():
        return None
    candidates = _selected_serving_ports(service_name)
    seen = {p.resolve() for p in candidates}
    candidates += [
        p for p in sorted(
            (p for p in recipe.iterdir()
             if p.is_file() and p.name.startswith(("serving_ports", "serving-ports"))),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if p.resolve() not in seen
    ]
    for path in candidates:
        try:
            rows = stream_rows(yaml.safe_load(path.read_text(encoding="utf-8", errors="replace")))
        except Exception:  # noqa: BLE001
            continue
        row = row_for_sub_service(rows, sub_service)
        if row is not None:
            return row.get("kind")
    return None


def _resolve_stream_row(entry: dict, service_name: str, sub_service: str) -> dict[str, Any] | None:
    """A sub-service's STREAM row, from the instance's RENDERED stream conf.

    G61 (D6): the exposed set is the rendered confs. The registry's
    ``stream_sub_services`` (parsed from the rendered file at deploy time) is
    the membership check — a sub-service filtered out by the active profiles is
    NOT in it, and minting it is refused. The port is read from the same
    rendered block; the registry stays ports-free.

    The old artifact scan (selected serving_ports, then newest-first) is gone as
    the authority: it still contains rows the conf does not serve.
    """
    import re

    recorded = entry.get("stream_sub_services") or []
    canonical = None
    for name in recorded:
        if "--" in name and name.split("--", 1)[1].split(".")[0] == sub_service:
            canonical = name
            break
    if canonical is None:
        return None

    paths: list[str] = []
    by_cat = entry.get("nginx_conf_paths_by_category") or {}
    if by_cat.get("stream"):
        paths.append(by_cat["stream"])
    for p in entry.get("nginx_conf_paths") or []:
        if p not in paths and (str(p).endswith(".stream.conf") or ".stream." in str(p)):
            paths.append(p)

    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # One block per sub-service: find the block naming this canonical name,
        # then read its upstream port.
        depth = 0
        block: list[str] = []
        in_target = False
        for line in text.splitlines():
            if depth == 0 and re.match(r"^[ \t]*server[ \t]*\{", line):
                block = [line]
                in_target = False
                depth = line.count("{") - line.count("}")
                continue
            if depth > 0:
                block.append(line)
                depth += line.count("{") - line.count("}")
                if f"server_name" in line and canonical in line:
                    in_target = True
                if depth <= 0:
                    if in_target:
                        body = "\n".join(block)
                        m = re.search(
                            r"proxy_pass[ \t]+(?:https?://)?(?:\{\{\s*container_prefix\s*\}\})?"
                            r"[A-Za-z0-9_.-]+:(\d+)",
                            body,
                        ) or re.search(
                            r"set[ \t]+\$[A-Za-z0-9_]+[ \t]+(?:\{\{\s*container_prefix\s*\}\})?"
                            r"[A-Za-z0-9_.-]+:(\d+)",
                            body,
                        )
                        if m:
                            return {
                                "sub_service": sub_service,
                                "port": int(m.group(1)),
                                "kind": _artifact_kind(service_name, sub_service),
                            }
                    block = []
    return None



def _owner_for_mint(db: Session, entry: dict, requester: dict) -> EndUser:
    """Who the handle will be recorded against — and whether the asker may mint.

    **Owner-only, or an admin acting on the owner's behalf** (R2.2). The recorded
    owner is always the *instance's user*, so the per-connection verdict
    (``stream_service.verify``) re-evaluates exactly the entitlement that
    mattered at mint time — an admin's own id never becomes a handle's owner.
    """
    instance_user_name = str(entry.get("user_name", ""))

    if requester.get("user_type") == "admin":
        owner = (
            db.query(EndUser)
            .filter(EndUser.username == instance_user_name)
            .first()
        )
        if owner is None:
            raise HTTPException(
                status_code=409,
                detail=f"Instance owner '{instance_user_name}' is not a registered end user",
            )
        return owner

    # An end user may mint for their own instance, or for one a special-user grant
    # covers them for — the SAME matrix the HTTP gate applies (R3.3).
    owner = db.query(EndUser).filter(EndUser.id == int(requester["id"])).first()
    if owner is None:
        raise HTTPException(status_code=401, detail="User not found")
    from ..services.auth_service import acl_allows

    if not acl_allows(db, owner, instance_user_name):
        raise HTTPException(status_code=403, detail="ACL denied")
    return owner


# ---------------------------------------------------------------------------
# The dashboard's surface
# ---------------------------------------------------------------------------


@router.post("/exposures")
async def mint_exposure(
    request: Request,
    payload: MintRequest,
    current_user: dict = Depends(require_gateway_token),
    db: Session = Depends(get_db),
):
    """Mint an exposure URL for one stream row and return it **once** (C3).

    What travels back is the client-facing name — ``<handle>.<domain>`` — and the
    store keeps only its hash, so this response is the only place it can ever be
    read (R2.1/R2.2). Minting is *revoke + insert*, so any previous URL for the
    same row dies at its next connection (R7.2).
    """
    entry = _instance_entry(request, payload.service_name, payload.user_name, payload.label)
    owner = _owner_for_mint(db, entry, current_user)

    # The row — and so the port AND the kind — comes from the artifact the deploy
    # used. This is what lets the card mint from a SUB-SERVICE name alone, which
    # is all the registry records.
    artifact_row = _resolve_stream_row(entry, payload.service_name, payload.sub_service)
    if artifact_row is None:
        raise HTTPException(
            status_code=409,
            detail=(f"sub-service {payload.sub_service!r} is not in "
                    f"{payload.service_name}'s rendered stream conf — nothing to expose"),
        )
    resolved_port = int(artifact_row["port"])
    if payload.port is not None and int(payload.port) != resolved_port:
        raise HTTPException(
            status_code=409,
            detail=(f"port {payload.port} disagrees with the serving_ports row "
                    f"({resolved_port}) — the artifact is the authority"),
        )

    host, row = stream_service.mint(
        db,
        owner_user_id=owner.id,
        entry=entry,
        sub_service=payload.sub_service,
        port=resolved_port,
    )
    address = f"{host}:{settings.STREAM_PORT}"
    return {
        "host": host,
        "stream_port": settings.STREAM_PORT,
        # THE COPYABLE VALUE: the name and the port, and nothing else.
        #
        # The PORT is the half that must not be lost — handed only the name, a
        # client dials its protocol default (7687 for Bolt), which is the
        # instance's INTERNAL port and answers nothing, and the operator concludes
        # the feature is broken (measured 2026-10-16, alongside a UI that copied
        # the bare host).
        #
        # The SCHEME is not the platform's to add. Nothing above the door is
        # protocol-aware (see `stream_conf`'s note): the bytes are opaque, `bolt`
        # and `redis` are not special to any code path, and a scheme this layer
        # invented for a kind it does not know would be a guess dressed as a fact.
        # The operator knows which protocol their row speaks, because it is the
        # protocol their client speaks.
        "address": address,
        "kind": artifact_row.get("kind"),
        "canonical_name": row.canonical_name,
        "row": {"sub_service": row.sub_service, "port": row.port},
        # What this says is a promise about the SERVER, not about the screen: the
        # store keeps only the hash, so no endpoint can hand the handle back
        # again. The dashboard is free to keep showing what it holds, and does.
        "shown_once": True,
    }


@router.get("/exposures")
async def list_exposures(
    request: Request,
    service_name: str = Query(...),
    user_name: str = Query(...),
    label: str = Query(...),
    current_user: dict = Depends(require_gateway_token),
    db: Session = Depends(get_db),
):
    """The recorded exposure rows of one instance — **never a handle**.

    The card reads this to decide what to offer (R6.1): the recorded state, not
    a front-end condition, is what makes an action appear.
    """
    _instance_entry(request, service_name, user_name, label)
    rows = stream_service.list_for_instance(db, service_name, user_name, label)
    return {"rows": [r.to_dict() for r in rows]}


@router.delete("/exposures")
async def revoke_exposure(
    request: Request,
    service_name: str = Query(...),
    user_name: str = Query(...),
    label: str = Query(...),
    sub_service: str = Query(...),
    port: int = Query(..., ge=1, le=65535),
    current_user: dict = Depends(require_gateway_token),
    db: Session = Depends(get_db),
):
    """Revoke the live handle of one stream row (idempotent).

    This deliberately does NOT require the instance to still be in the registry.
    The delete sweep (R7.1) revokes the instance's handles AFTER the instance is
    gone, so an instance check here would make the very case the sweep exists for
    impossible — measured 2026-10-10: revoking a just-deleted instance's row
    answered `404 No such instance`. The store, not the registry, is the authority
    on what there is to revoke.
    """
    revoked = stream_service.revoke_row(db, service_name, user_name, label, sub_service, port)
    return {"revoked": revoked}


# ---------------------------------------------------------------------------
# The edge's surface
# ---------------------------------------------------------------------------


@router.post("/verify")
async def verify_stream(
    request: Request,
    payload: VerifyRequest,
    db: Session = Depends(get_db),
):
    """The verdict for one connection (C1) — always 200, always a verdict.

    A *deny* is an answer, not an error, so it travels as ``allow: false`` with a
    reason; the edge denies on a non-200 **or** on ``allow: false`` (R4.2). Any
    failure inside is caught by the service and turned into a deny — this
    endpoint has no ``allow`` path that is not an explicit decision.
    """
    registry = getattr(request.app.state, "registry", None)
    return stream_service.verify(
        db,
        registry,
        payload.handle,
        requested_sub_service=payload.sub_service,
        requested_port=payload.port,
    )
