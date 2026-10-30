"""What counts as a probe having actually reached its target.

A probe script is written by a worker, so nothing it prints (a
"PROBE OK <url>" line, a status code, a response body) proves it ever
contacted anything. The engine instead runs every probe script behind
its own recording proxy (engine.infrastructure.probe_proxy) and keeps
one contact record per upstream host the script's traffic went to:

    {"host": str, "port": int, "method": str, "connected": bool,
     "bytes_up": int, "bytes_down": int, "response_sha256": str}

A target is reached iff some contact names the host of the target's
own address from facts.md ("## 외부 대상"), the proxy actually opened
the upstream connection, and the upstream sent at least one byte back.
These are pure functions over that record so the probe runner and the
evidence graph (which recomputes "ok" from the ledger payload rather
than trusting a stored flag) judge a probe the same way.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

_EMBEDDED_URL = re.compile(r"https?://[^\s`'\"<>()]+")


def _normalize_host(host: str) -> str:
    return host.strip().strip("[]").rstrip(".").lower()


def _url_host(value: str) -> str | None:
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = parts.hostname
    if not host:
        return None
    return _normalize_host(host)


def target_host(url: str) -> str | None:
    """Host the engine must observe traffic to for this target, or None
    when the target's facts.md value names no http(s) address at all
    (such a target cannot be observed and therefore never passes). The
    value is normally a bare URL; an http(s) address embedded in a
    longer value is used when the value itself is not one."""
    direct = _url_host(url.strip())
    if direct is not None:
        return direct
    match = _EMBEDDED_URL.search(url)
    if match is None:
        return None
    return _url_host(match.group(0))


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return 0


def contact_reaches(contact: Mapping[str, object], host: str) -> bool:
    raw_host = contact.get("host")
    if not isinstance(raw_host, str):
        return False
    return (_normalize_host(raw_host) == host
            and contact.get("connected") is True
            and _as_int(contact.get("bytes_down")) > 0)


def target_port(url: str) -> int | None:
    """The target's port: explicit in the URL, else 443 (https) / 80 (http)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if port is not None:
        return port
    return {"https": 443, "http": 80}.get((parts.scheme or "").lower())


def target_reached(url: str, contacts: Iterable[object]) -> bool:
    """True iff `contacts` (the proxy's records for ONE run of this
    target's script) show a real response from the target's host AND
    port -- a responder the probe started on another port of the same
    host never counts."""
    host = target_host(url)
    port = target_port(url)
    if host is None or port is None:
        return False
    return any(isinstance(c, Mapping) and contact_reaches(c, host)
               and _as_int(c.get("port")) == port for c in contacts)


__all__ = ["target_host", "target_port", "contact_reaches", "target_reached"]
