"""The internal stream conf — one `server{}` per STREAM row (task-doc T9 / R5.1).

This module is the generator. It is deliberately **mechanical**: the *judgment*
(which ports exist and what each speaks) belongs to the `serving_ports` artifact
and its LLM inference; what is left here is arithmetic — pick the rows whose
`kind` resolves to the `stream` category, name each one, and write a block the
internal nginx can select by `server_name`.

The shape it emits is the one the -api's own conversion expects
(`convert_stream_conf`, R9.2), and the two are a pair: the conf carries the
canonical name verbatim (the edge dials exactly that as the inner hop's SNI —
R4.4), the container by its COMPOSE service name (the -api prefixes it with
`{{ container_prefix }}`), and the certificate path from the deploy's own store
(the -api turns it into `{{ ssl_certificate_path }}`). Nothing here needs to know
any of those substitutions — it writes the plain conf, and the -api converts it.

What this file must NOT write, and why (each is a measured defect avoided):

* **no `auth_basic`** — the stream module has no such directive; injecting one
  makes the conf pass `nginx -t` for an unprotected instance and FAIL for a
  password-protected one;
* **no ssl guard** — a stream block's TLS listener is structural, and the FILE'S
  EXISTENCE is the switch (R8.4/R9.3), so the block is never conditional;
* **no `map`** — the canonical names are known when this conf is generated, so
  each block carries its own certificate (R4.3's per-listener rule).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .serving_ports import category_of

# The internal listener's container port (task-doc R5.1: the port the compose
# publishes; the container port is an implementation detail no client sees).
INTERNAL_STREAM_PORT = 7687

# The shared network the internal nginx sits on, which is the only source allowed
# to reach this listener. Deliberately NOT a door control (R5.2): a client
# arriving through a published port passes docker's NAT and can appear inside
# this range, so the guard is tenant isolation and nothing more.
SHARED_SUBNET = "172.17.0.0/16"

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


def render_stream_conf(
    rows: list[dict[str, Any]],
    *,
    service_name: str,
    user_name: str,
    label: str,
    domain: str,
    cert_pem: str,
    cert_key: str,
) -> str:
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
            # NO `ssl_preread` here, deliberately: the static upstream below is
            # enough for nginx to choose the block by the SNI (measured on a
            # minimal reproduction — two blocks, one certificate, two upstreams,
            # and the SNI selected correctly with and without the line). The
            # defect this block had was the upstream FORM, not a missing preread.
            #
            # G56 (D7) — TEMPLATED, like the http category's `server_name`. The
            # -api renders `hostname_stem` and `domain_name` for the target
            # instance, so a conf selected from another deployment is portable and
            # the gateway never bakes an instance identity into a recipe artifact.
            f"    server_name         {{{{ hostname_stem }}}}--{sub}.{{{{ domain_name }}}};\n"
            f"\n"
            f"    # The domain wildcard, named per block: these names are known when\n"
            f"    # this conf is generated, so no map is needed (R4.3/R5.4).\n"
            f"    ssl_certificate     {cert_pem};\n"
            f"    ssl_certificate_key {cert_key};\n"
            f"\n"
            f"    # Tenant isolation, NOT a door control (R5.2).\n"
            f"    allow               {SHARED_SUBNET};\n"
            f"    deny                all;\n"
            f"\n"
            # The last hop names the instance's OWN container, and it is STATIC —
            # deliberately NOT `set $stream_upstream <name>; proxy_pass
            # $stream_upstream;` with a `resolver`.
            #
            # The variable form DEFEATS the SNI-based choice of virtual server: every
            # connection lands on the FIRST block, so every row but the first is
            # unreachable. Measured 2026-10-16 on a minimal reproduction — one nginx,
            # one certificate, two blocks, two distinguishable upstreams:
            #
            #   static proxy_pass          -> SNI a.test hit block 1, b.test hit block 2
            #   set + variable + resolver  -> BOTH hit block 1
            #
            # …and on the live door, one handle per row: under the variable form the
            # falkordb name answered NOTHING — the bytes reached neo4j's Bolt listener,
            # which dropped them (`TCP 200, read 0`), and FalkorDB's MONITOR showed the
            # command never arrived — while under this one it answered `+PONG` from
            # FalkorDB itself. `ssl_preread` is NOT the missing piece: the static form
            # selects correctly without it (measured).
            #
            # The price is R5.3's: nginx resolves a static upstream at LOAD time, so a
            # conf left behind by a deleted instance makes the next reload fail. That
            # is the cost of the routing working at all — the alternative is rows that
            # cannot be reached.
            #
            # `{{ container_prefix }}` is substituted by the -api at register time
            # (its own naming, one home in `template_engine.container_prefix`), which
            # is what keeps this the instance's own container rather than a name every
            # instance shares. Its converter leaves this line alone: it rewrites
            # `<name>` in a `set $stream_upstream` line, and this is not one.
            f"    proxy_pass          {{{{ container_prefix }}}}{sub}:{port};\n"
            f"}}\n"
        )
    if not blocks:
        return ""
    header = (
        "# Generated by the provision gateway (one block per stream row).\n"
        "# The -api converts and renders this file; the edge dials each block by the\n"
        "# canonical name in its `server_name`.\n"
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
    ssl_dir: str,
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

    content = render_stream_conf(
        rows,
        service_name=service_name, user_name=user_name, label=label, domain=domain,
        cert_pem=str(Path(ssl_dir) / domain / "fullchain.pem"),
        cert_key=str(Path(ssl_dir) / domain / "privkey.pem"),
    )
    if not content:
        return None
    out = Path(recipe_dir) / f"{service_name}.user-{user_name}.{label}.stream.conf"
    try:
        out.write_text(content, encoding="utf-8")
        Path(str(out) + ".generated").write_text("")
    except OSError:
        return None
    return str(out)
