"""Small shared predicates for network-address classification."""

from __future__ import annotations

import ipaddress


def is_loopback_host(host: str) -> bool:
    """Return whether ``host`` identifies the local loopback interface.

    Host names are normalized for case and an optional DNS trailing dot. IP
    parsing intentionally delegates to :mod:`ipaddress`, which correctly
    handles the full IPv4 127/8 range and IPv6 loopback.
    """

    normalized = str(host).strip().lower().rstrip(".")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False
