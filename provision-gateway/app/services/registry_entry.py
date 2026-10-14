"""Reading a registry entry's flags — the record's vocabulary, in one place.

The registry is written by the -api and read here (the gateway's hostname index
and its status routes). A field whose NAME matters to both sides belongs in one
reader, so neither service keeps its own copy of the rule.

`entry_tls` is the first of those. The toggle used to be stored as `https` and is
now `tls` — the same word the deploy request, the CLI and the template variable
use (ruled 2026-10-10). Entries written before the rename are read through a
fallback kept **for one round**; the -api has the mirror of this function in
`lib/registry.py`, and the two must agree.
"""

from __future__ import annotations

from typing import Any


def entry_tls(entry: dict[str, Any] | None) -> bool:
    """Is TLS in play for this instance?

    Reads the record's ``tls`` switch, falling back to the pre-rename name
    ``https`` for one round. Anything unreadable is ``False`` — fail-closed, so an
    instance whose flag cannot be read gets no TLS vhost and no stream conf rather
    than an accidental one.
    """
    if not isinstance(entry, dict):
        return False
    if "tls" in entry:
        return bool(entry.get("tls"))
    return bool(entry.get("https"))
