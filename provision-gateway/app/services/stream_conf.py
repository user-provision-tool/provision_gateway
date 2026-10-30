"""The internal stream conf — one `server{}` per STREAM row (task-doc T9 / R5.1).

This module is the generator. It is deliberately **mechanical**: the *judgment*
(which ports exist and what each speaks) belongs to the `serving_ports` artifact
and its LLM inference; what is left here is arithmetic — pick the rows whose
`kind` resolves to the `stream` category, name each one, and write a block the
internal nginx can select by `server_name`.

The shape it emits is the one the -api's own conversion expects
(`convert_stream_conf`, R9.2), and the two are a pair: the conf carries a MOCK
`server_name` (the -api rebuilds it from `<instance>--<sub>.<domain>`), a PLAIN
compose-service upstream (the -api prefixes it with `{{ container_prefix }}`),
and neither a certificate nor an access policy (the -api injects both). Nothing
here needs to know any instance identity or the platform's network — it writes a
plain, portable artifact, and the -api converts it.

What this file must NOT write, and why (each is a measured defect avoided):

* **no `auth_basic`** — the stream module has no such directive; injecting one
  makes the conf pass `nginx -t` for an unprotected instance and FAIL for a
  password-protected one;
* **no ssl guard** — a stream block's TLS listener is structural, and the FILE'S
  EXISTENCE is the switch (R8.4/R9.3), so the block is never conditional;
* **no certificate line** — the deploy pair is injected by the -api, the one
  owner of certificate material; a generated path could only be a guess;
* **no `allow`/`deny`** — the platform subnet is a property of the stack the conf
  is deployed into, never of the recipe, and the gateway cannot see that stack.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .serving_ports import category_of

# The internal listener's container port (task-doc R5.1: the port the compose
# publishes; the container port is an implementation detail no client sees).
INTERNAL_STREAM_PORT = 7687

# The docker embedded DNS is deliberately NOT used here any more: the blocks
# upstream STATICALLY (see `render_stream_conf`), because the variable form that
# needed a resolver is what stopped the SNI selecting the block.

# NOTE — the platform does NOT name the stream protocol, and there is deliberately
# no `kind -> URL scheme` table here.
#
# One existed briefly (2026-10-16, added by the assistant) and was removed on the
# operator's ruling: *"stop showing the protocol on the dashboard; it will be the
# user's responsibility to know which protocol to call the stream API."* The
# platform's own rule says the same thing in another form — "the kind is not the
# port", and the only load-bearing table is `kind -> CATEGORY`. Everything at or
# above the door is protocol-blind: the bytes are opaque, so `bolt` and `redis`
# are not special to any code path, and a scheme invented for a kind nobody has
# heard of (`postgres+s://`?) would be a guess dressed as a fact.
#
# What travels to the operator is the NAME AND THE PORT. The client supplies its
# own protocol's spelling, which is exactly the knowledge the operator has and the
# platform does not.


def stream_rows(serving_ports: Any) -> list[dict[str, Any]]:
    """The rows of a `serving_ports` artifact whose kind is a STREAM protocol.

    R1.4: these rows ARE the feature's subject — no stream row means no
    exposeable target. A row with no `kind` is not exposed (fail-closed, R1.2),
    so it is not a stream row either.
    """
    if not isinstance(serving_ports, dict):
        return []
    rows = serving_ports.get("ports")
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if category_of(row.get("kind")) != "stream":
            continue
        sub = row.get("sub_service")
        port = row.get("port")
        if not isinstance(sub, str) or not sub or not isinstance(port, int):
            continue
        out.append(row)
    return out


def canonical_name(*, service_name: str, user_name: str, label: str, domain: str,
                   sub_service: str) -> str:
    """`<instance host>--<sub_service>.<domain>` — the name the edge dials.

    One definition, shared with the gateway's exposure service so the minted
    handle's stored name and the generated block's `server_name` cannot drift.
    """
    return f"{service_name}-{user_name}-{label}--{sub_service}.{domain}"


def render_stream_conf(rows: list[dict[str, Any]]) -> str:
    """The conf text for one instance's stream rows — one `server{}` per row.

    Empty when there are no stream rows: the caller writes no file at all, which
    is what makes "no stream row ⇒ nothing exposed" true by construction rather
    than by a check somewhere downstream.
    """
    blocks: list[str] = []
    for row in rows:
        sub = str(row["sub_service"])
        port = int(row["port"])
        blocks.append(
            f"server {{\n"
            f"    listen              {INTERNAL_STREAM_PORT} ssl;\n"
            # A MOCK name, in the shape the stream converter parses:
            # `<anything>--<sub>.<domain>`. `.invalid` is reserved and can never
            # resolve, so the mock cannot be mistaken for a real endpoint; the
            # -api rebuilds the name from the instance it renders for.
            #
            # NO `ssl_preread` here, deliberately: the static upstream below is
            # enough for nginx to choose the block by the SNI (measured on a
            # minimal reproduction — two blocks, one certificate, two upstreams,
            # and the SNI selected correctly with and without the line).
            f"    server_name         mock_server_name--{sub}.mock.invalid;\n"
            f"\n"
            # The last hop names the recipe's OWN compose service, STATICALLY —
            # deliberately NOT `set $stream_upstream <name>; proxy_pass
            # $stream_upstream;` with a `resolver`. The variable form DEFEATS the
            # SNI-based choice of virtual server (every connection lands on the
            # FIRST block; measured 2026-10-16), and it is also the form nginx can
            # evaluate empty. `{{ container_prefix }}` is the -api's substitution:
            # it rewrites this host at conversion time, so the rendered conf names
            # the instance's own container.
            f"    proxy_pass          {sub}:{port};\n"
            f"}}\n"
        )
    if not blocks:
        return ""
    header = (
        "# Generated by the provision gateway (one block per stream row).\n"
        "# Mock name and plain upstream: the -api rewrites the name, prefixes the\n"
        "# upstream, and injects the deploy certificate and the platform policy.\n"
    )
    return header + "\n" + "\n".join(blocks)


def row_for_sub_service(rows: list[dict[str, Any]], sub_service: str) -> dict[str, Any] | None:
    """The STREAM row of one sub-service, for the card's mint (R6.1/R6.2).

    The registry stays PORTS-FREE (§9's ruling: the entry records canonical
    NAMES), so the card cannot send a port — it knows the sub-service. The port
    is therefore resolved here, from the artifact the deploy actually used, which
    is also the only place that knows it. A sub-service with no stream row
    resolves to None, and the caller refuses: the card offers a row only for a
    recorded stream sub-service, so a miss means the artifact and the entry
    disagree, which is a refusal and not a guess.
    """
    for row in rows:
        if str(row.get("sub_service")) == sub_service:
            return row
    return None


def produce_for_deploy(
    *,
    service_name: str,
    user_name: str,
    label: str,
    domain: str,
    serving_ports_path: str | None,
    recipe_dir: str,
) -> str | None:
    """Write this instance's stream conf for ONE deploy; return its path or None.

    **Why this is a deploy-time step and not (only) a pipeline phase** — measured
    2026-10-10, while running the end-to-end plan: a deploy whose files are ALL
    selected runs **no generation at all** (the panel says "all necessary files
    are specified" and the request goes straight to the -api). A stream conf that
    only a generation phase produces therefore never exists for the normal,
    fully-configured recipe — and the whole exposure path is unreachable. It is
    the same shape as the per-user env, which is why the env is generated "as the
    last step of the deploy pipeline": some artifacts belong to the DEPLOY.

    Returns None (writing nothing) when there is nothing to expose — no artifact,
    no stream row — which is R1.4 by construction.
    """
    import yaml

    if not serving_ports_path:
        return None
    p = Path(serving_ports_path)
    if p.is_absolute() is False:
        p = Path(recipe_dir) / p
    if not p.is_file():
        return None
    try:
        rows = stream_rows(yaml.safe_load(p.read_text(encoding="utf-8", errors="replace")))
    except Exception:  # noqa: BLE001 — an unreadable artifact exposes nothing
        return None
    if not rows:
        return None

    content = render_stream_conf(rows)
    if not content:
        return None
    out = Path(recipe_dir) / f"{service_name}.user-{user_name}.{label}.stream.conf"
    try:
        out.write_text(content, encoding="utf-8")
        Path(str(out) + ".generated").write_text("")
    except OSError:
        return None
    return str(out)
