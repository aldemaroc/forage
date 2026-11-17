"""Outbound target policy: private, loopback, link-local and cloud metadata.

Extraction fetches URLs chosen by the caller (a model, or a page that injected
a prompt). httpx and the browser follow redirects on their own, so a public URL
can land on the LAN, on the Docker host gateway or on a cloud metadata
endpoint and Forage would hand the body back. This module answers one question
- may Forage dial this target? - and it is applied before every fetch, on
every redirect hop and on the URL the browser ends up at.

``extract.allow_private_targets: true`` lifts the check for private ranges.
Cloud metadata endpoints stay blocked either way.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit

logger = logging.getLogger(__name__)

_HTTP_SCHEMES = frozenset({"http", "https"})

_METADATA_HOSTNAMES = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
    }
)

# IMDS addresses: AWS/GCP/Azure/OpenStack, AWS over IPv6, Alibaba Cloud.
_METADATA_IPS = frozenset({"169.254.169.254", "fd00:ec2::254", "100.100.100.200"})


class TargetBlocked(Exception):
    """The outbound target was refused by policy."""


def _ip_reason(ip: Any, allow_private: bool) -> Optional[str]:
    if str(ip) in _METADATA_IPS:
        return "cloud metadata endpoint"
    if allow_private:
        return None
    if ip.is_multicast:
        return "multicast address"
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_unspecified:
        return "unspecified address"
    if ip.is_private:
        return "private address"
    if ip.is_reserved:
        return "reserved address"
    if not ip.is_global:
        return "non-public address"
    return None


def _resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


def block_reason(url: str, *, allow_private: bool) -> Optional[str]:
    """Reason the target is refused, or None when it may be dialled.

    Every DNS answer is checked, so a name that resolves to one public and one
    private address is refused. A name that does not resolve is refused too:
    the request could not be made safely, and a proxy would resolve it instead.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "unparseable URL"
    scheme = (parts.scheme or "").lower()
    if scheme not in _HTTP_SCHEMES:
        return f"unsupported URL scheme {scheme or '<empty>'!r}"
    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        return "URL with no host"
    if host in _METADATA_HOSTNAMES:
        return "cloud metadata endpoint"

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return _ip_reason(literal, allow_private)

    if not allow_private and (host == "localhost" or host.endswith(".localhost")):
        return "loopback hostname"

    try:
        addresses = _resolve(host)
    except OSError as exc:
        return f"unresolvable host ({exc.__class__.__name__})"
    if not addresses:
        return "unresolvable host"
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw.split("%")[0])
        except ValueError:
            return "unparseable resolved address"
        reason = _ip_reason(ip, allow_private)
        if reason:
            return reason
    return None


def _message(url: str, reason: str, allow_private: bool) -> str:
    host = urlsplit(url).hostname or url
    if reason == "cloud metadata endpoint":
        return f"Blocked by policy: {host} is a cloud metadata endpoint (always blocked)"
    base = f"Blocked by policy: {host} is a {reason}"
    if allow_private:
        return base
    return base + ". Set extract.allow_private_targets: true in the Forage config to allow it."


def _check(url: str, allow_private: bool) -> None:
    reason = block_reason(url, allow_private=allow_private)
    if reason:
        raise TargetBlocked(_message(url, reason, allow_private))


async def ensure_allowed(url: str, *, allow_private: bool) -> None:
    """Raise ``TargetBlocked`` when the target is refused.

    The DNS lookup is blocking, so it runs in a worker thread: extraction is
    async and one slow resolver would otherwise stall the whole worker.
    """
    await asyncio.to_thread(_check, url, allow_private)


async def get_following_redirects(
    client: Any,
    url: str,
    *,
    allow_private: bool,
    headers: Optional[dict] = None,
    max_redirects: int = 10,
) -> Any:
    """GET with redirects followed by hand, so each hop is checked.

    ``follow_redirects=True`` would let the server decide where the request
    goes after the first check. Each ``Location`` is resolved against the
    response URL and validated before it is dialled.
    """
    current = url
    for _ in range(max_redirects + 1):
        await ensure_allowed(current, allow_private=allow_private)
        resp = await client.get(current, headers=headers, follow_redirects=False)
        if not resp.is_redirect:
            return resp
        location = resp.headers.get("location")
        if not location:
            return resp
        current = urljoin(str(resp.url), location)
    raise TargetBlocked(f"Blocked by policy: more than {max_redirects} redirects from {url}")
