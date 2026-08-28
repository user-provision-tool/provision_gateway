# Edge `-nginx-acl` security notes (decision 17)

The v5 edge `-nginx-acl` is the **only externally-exposed nginx** in fullset.
It terminates TLS for every service domain using certs from `SSL_DIR`
(mounted read-only at `/certs`). That makes the edge the **holder of every
service private key** — a single compromise exposes every service's TLS
identity. This document records the mitigations and the residual risk.

## The key-holder risk

- The edge decrypts all service traffic (SNI → `/certs/{domain}/privkey.pem`),
  so it can read plaintext on the internal path after termination (it also
  re-encrypts to the internal `-nginx`, but that double-TLS is about
  protecting the internal path, not the edge itself).
- The edge is internet-facing. If it is compromised, the attacker obtains:
  - every service private key under `SSL_DIR`,
  - the plaintext of every proxied request (it sits in the TLS path),
  - the `X-Service-Basic` credentials it injects (relayed from the gateway).

## Mitigations in this implementation

1. **Read-only mount** — `SSL_DIR` is mounted `:ro` at `/certs`, so a
   compromised edge cannot tamper with the certs (rotate on the host).
2. **Non-root** — the edge runs as UID `nginx` (`user: nginx`, decision 17),
   binding 80/443 via the default `NET_BIND_SERVICE` docker capability. It
   cannot write most of the container filesystem.
3. **Placeholder cert kept OUT of the mount** — the startup placeholder lives
   in the edge **image** at `/etc/nginx/acl/placeholder/` (decision 11), not
   under the read-only `/certs` mount (the mount would hide image content and
   the image cannot write there anyway).
4. **Certs are the user's concern** — the system only mounts `SSL_DIR` and
   serves what the operator places there (§7). There is no system-side cert
   provisioning API for the edge.
5. **`proxy_ssl_verify off` is internal-only** (decision 14) — the edge→
   internal leg is still TLS-encrypted; only the internal cert identity is
   unverified, which is acceptable on the trusted `subnet_acl_shared` network.

## Residual risk / operator guidance

- **Rotate keys on a compromise** — with `SSL_DIR` `:ro`, place new certs on
  the host and they are picked up per-handshake by the dynamic SNI map (no
  edge restart, decision 9). Do NOT trust a rotated key that only ever lived
  inside a compromised container.
- **The edge IS the trust boundary for TLS** — restricting access to the edge
  container (`docker` socket access, host access) is as important as
  restricting the gateway.
- **Do not place `SSL_DIR` anywhere untrusted** — it should live on the
  provision host, owned by the operator, readable by the edge via the
  read-only mount only.
- **The gateway is the auth authority, the edge is the gate** — the edge
  relays `Authorization: Basic $service_basic` obtained from the gateway's
  `verify` response. It has no independent auth state; keep
  `GATEWAY_SECRET_KEY` and the gateway unreachable-from-outside (the edge's
  `/_auth_jwt` fail-closes with a 500 if the gateway is down, decision 3).

## Wrong-scheme behavior (U1) — client error, not a security control

- A certed service reached over http gets a **301** to https (force-https,
  decision 18).
- https on an **http-only** service → **502** after edge termination (the
  internal `-nginx` has no 443 server block for it).
- https-provisioned service **without a cert** → **TLS handshake fails** at
  the edge (decision 9, certless-https: the SNI map resolves to a missing file).

These are deliberate client-error behaviors; no defensive code is added.
