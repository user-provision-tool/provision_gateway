"""StreamExposure ORM model — the opaque exposure handle for one stream row.

One row per minted exposure URL. The handle itself is stored **only as its
SHA-256 hash**, so this store can verify a handle and can never reproduce one
(task-doc §3.6 / §5.2). There is **no TTL**: a handle lives until it is
regenerated for its row, or its instance/row is deleted (GRILLING **G39** §7).

The row shape mirrors the ER diagram in the task doc's §3.6, with two fields
that carry the same meaning as they do in ``api_keys``:

``owner_user_id``
    the end user whose entitlement the verdict re-evaluates **per connection**
    (R3.3) — an admin minting on someone's behalf records *that user*, never
    themselves (R2.2).
``user_name``
    the **instance's** registry user — the ACL matrix's target side, so the
    matrix is one comparison: ``user_name == owner.username`` means "their own
    instance", and the owner's ``allowed_special_users`` grants the rest.

The exposure URL's label is the handle, so the handle must stay a single DNS
label; ``canonical_name`` is therefore *not* the client-facing name but the
platform-internal one the inner hop carries (`<instance host>--<sub>.<domain>`),
which is also the generated stream block's ``server_name`` (R5.1/R9.2).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, text

from ..database import Base


class StreamExposure(Base):
    """A minted exposure handle bound to ONE stream row of ONE instance."""

    __tablename__ = "stream_exposures"

    # ONE live handle per (instance, row) — R2.2's "minting a new handle for a
    # row revokes the previous one". A partial unique index, exactly like
    # api_keys' one-default rule, so the invariant cannot be broken by a caller
    # that forgets to revoke first.
    __table_args__ = (
        Index(
            "uq_stream_exposures_one_live_per_row",
            "service_name", "user_name", "label", "sub_service", "port",
            unique=True,
            sqlite_where=text("revoked_at IS NULL"),
        ),
    )

    # The natural key. A verify is a single primary-key hit — the hot path, since
    # the edge's njs hook calls it once per connection.
    handle_hash = Column(String(64), primary_key=True)
    owner_user_id = Column(Integer, ForeignKey("end_users.id"), nullable=False, index=True)

    # The bound instance, as the -api's registry keys it.
    service_name = Column(String(255), nullable=False)
    user_name = Column(String(255), nullable=False)
    label = Column(String(64), nullable=False)

    # The bound ROW — the scope check's pair: a handle minted for one stream row
    # can never open another (R3.2).
    sub_service = Column(String(255), nullable=False)
    port = Column(Integer, nullable=False)

    # `<instance host>--<sub_service>.<domain>`: the inner hop's SNI and the
    # generated stream block's server_name, verbatim.
    canonical_name = Column(String(255), nullable=False)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    revoked_at = Column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        """The row as the API may show it — never the handle, not even hashed."""
        return {
            "service_name": self.service_name,
            "user_name": self.user_name,
            "label": self.label,
            "sub_service": self.sub_service,
            "port": self.port,
            "canonical_name": self.canonical_name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "live": self.revoked_at is None,
        }
