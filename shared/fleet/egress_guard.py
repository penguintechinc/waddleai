"""Shared SSRF egress guard for fleet providers that dial admin-supplied URLs.

Centralizes HTTPS-only parsing, DNS vetting (every A/AAAA, transition-embedded
IPv4 decoded, global-unicast only) and a connect-time pinned resolver, so every
provider applies identical protection instead of hand-rolled copies.
"""

import asyncio
import socket
from collections.abc import Iterable
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address
from urllib.parse import urlparse

from aiohttp.abc import AbstractResolver, ResolveResult

_CGNAT = IPv4Network("100.64.0.0/10")
_NAT64 = IPv6Network("64:ff9b::/96")
_NAT64_LOCAL = IPv6Network("64:ff9b:1::/48")


def _candidates(address: IPv4Address | IPv6Address) -> list[IPv4Address | IPv6Address]:
    """Return the address plus every IPv4 embedded in it by a transition scheme.

    Decodes IPv4-mapped, NAT64 (64:ff9b::/96, 64:ff9b:1::/48), 6to4 and Teredo
    (both server and client) so each embedded target is vetted too.
    """
    found: list[IPv4Address | IPv6Address] = [address]
    if isinstance(address, IPv6Address):
        if address.ipv4_mapped is not None:
            found.append(address.ipv4_mapped)
        if address in _NAT64 or address in _NAT64_LOCAL:
            found.append(IPv4Address(int(address) & 0xFFFFFFFF))
        if address.sixtofour is not None:
            found.append(address.sixtofour)
        if address.teredo is not None:
            found.extend(address.teredo)
    return found


def is_forbidden(address: IPv4Address | IPv6Address) -> bool:
    """Return True unless the address and all embedded IPv4s are public unicast.

    Allow-style: every candidate must be globally routable, not multicast,
    loopback, link-local, unspecified or reserved, and not CGNAT 100.64.0.0/10.
    """
    for candidate in _candidates(address):
        if candidate.is_multicast or candidate.is_unspecified or candidate.is_loopback:
            return True
        if candidate.is_link_local or candidate.is_reserved or candidate.is_private:
            # NAT64 well-known prefix itself is flagged non-global; its embedded v4 is vetted.
            if not (isinstance(candidate, IPv6Address) and candidate in _NAT64):
                return True
        if isinstance(candidate, IPv4Address) and candidate in _CGNAT:
            return True
        if not candidate.is_global and not (
            isinstance(candidate, IPv6Address) and candidate in _NAT64
        ):
            return True
    return False


def forbidden_message(label: str) -> str:
    """Return the rejection message for a non-public target."""
    return f"{label} endpoint_url must not target a private or local address"


def validate_https_url(raw: str, label: str, allowed_suffixes: Iterable[str] | None = None) -> str:
    """Validate a credential-free HTTPS URL; return it without trailing slash.

    Literal IPs must be public. When allowed_suffixes is given the host must be
    a DNS name ending in one of them (so IP literals are rejected outright).
    """
    url = str(raw or "").rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{label} endpoint_url must be a credential-free HTTPS URL")
    host = parsed.hostname.lower()
    try:
        address = ip_address(host)
    except ValueError:
        address = None
    if allowed_suffixes is not None:
        suffixes = tuple(allowed_suffixes)
        if address is not None or not any(
            host.endswith(s) and len(host) > len(s) for s in suffixes
        ):
            raise ValueError(f"{label} endpoint_url host must end with one of {sorted(suffixes)}")
    elif address is not None and is_forbidden(address):
        raise ValueError(forbidden_message(label))
    return url


async def resolve_public_addresses(host: str, port: int, label: str = "Endpoint") -> list[str]:
    """Resolve a host (off the event loop) and return its addresses if all are public.

    Raises ValueError when the name has no addresses or ANY address is
    non-public, so a mixed public/private record set cannot be used for SSRF.
    """
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"{label} endpoint_url host {host!r} did not resolve") from exc
    addresses: list[str] = []
    for info in infos:
        text = str(info[4][0]).split("%", 1)[0]
        if is_forbidden(ip_address(text)):
            raise ValueError(forbidden_message(label))
        if text not in addresses:
            addresses.append(text)
    if not addresses:
        raise ValueError(f"{label} endpoint_url host {host!r} did not resolve")
    return addresses


class PinnedResolver(AbstractResolver):
    """aiohttp resolver that re-vets DNS at connect time and pins the vetted IPs.

    The connector dials exactly the addresses returned here, so a DNS rebind
    after validation cannot swap in a private target. The original hostname is
    kept so the Host header, TLS SNI and certificate validation still use it.
    """

    def __init__(self, label: str = "Endpoint") -> None:
        """Remember the provider label used in rejection messages."""
        self._label = label

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        """Resolve and vet host, returning only vetted addresses for the connector."""
        results: list[ResolveResult] = []
        for text in await resolve_public_addresses(host, port, self._label):
            address = ip_address(text)
            results.append(
                {
                    "hostname": host,
                    "host": text,
                    "port": port,
                    "family": socket.AF_INET6 if address.version == 6 else socket.AF_INET,
                    "proto": 0,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        return results

    async def close(self) -> None:
        """Release resources; the resolver holds none."""
