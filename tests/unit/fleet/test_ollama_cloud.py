"""Tests for the hosted Ollama register-and-route backend."""

import socket
from ipaddress import ip_address
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.fleet.base import BackendType, ManagementScope, ProvisionSpec
from shared.fleet.ollama_cloud import (
    OllamaCloudFleetBackend,
    _is_forbidden,
    _PinnedResolver,
    resolve_public_addresses,
)

PUBLIC = "104.18.7.1"


def _gai(*addrs):
    """Build a getaddrinfo-style result list for the given IP strings."""
    return [
        (
            socket.AF_INET6 if ":" in a else socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            (a, 443) if ":" not in a else (a, 443, 0, 0),
        )
        for a in addrs
    ]


@pytest.fixture(autouse=True)
def _public_dns():
    """Default every hostname lookup to a public address."""
    with patch("shared.fleet.ollama_cloud.socket.getaddrinfo", return_value=_gai(PUBLIC)) as gai:
        yield gai


def _session(status=200, payload=None):
    response = AsyncMock(status=status)
    response.json = AsyncMock(return_value=payload or {"models": [{"name": "gemma4:e4b"}]})
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get.return_value = response
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _backend():
    return OllamaCloudFleetBackend(
        None, config={"endpoint_url": "https://ollama.com", "name": "hosted"}, credentials="secret"
    )


def test_type_scope_and_https_boundary():
    """Verify: type scope and https boundary."""
    assert _backend().type == BackendType.OLLAMA_CLOUD
    assert _backend().management_scope == ManagementScope.REGISTER_AND_ROUTE
    with pytest.raises(ValueError, match="HTTPS"):
        _ = OllamaCloudFleetBackend(
            None, config={"endpoint_url": "http://host"}, credentials="x"
        )._base_url
    with pytest.raises(ValueError, match="private or local"):
        _ = OllamaCloudFleetBackend(
            None, config={"endpoint_url": "https://127.0.0.1:11434"}, credentials="x"
        )._base_url


async def test_models_route_with_bearer_auth():
    """Verify: models route with bearer auth."""
    backend = _backend()
    with patch("aiohttp.ClientSession", return_value=_session()) as client:
        endpoints = await backend.endpoints_for("gemma4:e4b")
    assert endpoints[0].node_id == "hosted"
    assert client.call_args.kwargs["headers"] == {"Authorization": "Bearer secret"}


@pytest.mark.parametrize("method", ["provision", "deprovision"])
async def test_lifecycle_is_rejected(method):
    """Verify: lifecycle is rejected."""
    backend = _backend()
    with pytest.raises(PermissionError, match="register_and_route"):
        if method == "provision":
            await backend.provision(ProvisionSpec("x", [], "cloud", {}))
        else:
            await backend.deprovision("x")


@pytest.mark.parametrize(
    "bad", ["10.1.2.3", "127.0.0.1", "169.254.169.254", "100.64.1.1", "::1", "::ffff:10.0.0.1"]
)
async def test_dns_name_resolving_to_forbidden_address_rejected(bad, _public_dns):
    """Verify: a hostname resolving to a non-public address is rejected (SSRF)."""
    _public_dns.return_value = _gai(bad)
    with pytest.raises(ValueError, match="private or local"):
        await resolve_public_addresses("evil.example", 443)
    with patch("aiohttp.ClientSession", return_value=_session()):
        assert await _backend().endpoints_for("gemma4:e4b") == []


async def test_any_private_address_among_many_rejected(_public_dns):
    """Verify: one private record among public ones rejects the whole name."""
    _public_dns.return_value = _gai(PUBLIC, "10.0.0.5")
    with pytest.raises(ValueError, match="private or local"):
        await resolve_public_addresses("mixed.example", 443)


async def test_public_address_accepted():
    """Verify: a public resolution is accepted and returned."""
    assert await resolve_public_addresses("ollama.com", 443) == [PUBLIC]


async def test_rebind_between_validate_and_request_rejected(_public_dns):
    """Verify: second resolution returning a private IP is rejected at connect time."""
    _public_dns.return_value = _gai(PUBLIC)
    assert await resolve_public_addresses("rebind.example", 443) == [PUBLIC]
    _public_dns.return_value = _gai("169.254.169.254")
    with pytest.raises(ValueError, match="private or local"):
        await _PinnedResolver().resolve("rebind.example", 443)


async def test_pinned_resolver_returns_vetted_ip_with_original_hostname():
    """Verify: resolver yields the vetted IP while keeping the hostname for SNI/Host."""
    result = await _PinnedResolver().resolve("ollama.com", 443)
    assert result[0]["host"] == PUBLIC
    assert result[0]["hostname"] == "ollama.com"
    assert result[0]["port"] == 443


async def test_models_uses_pinned_connector_and_no_redirects():
    """Verify: the request session pins DNS via the vetted resolver and refuses redirects."""
    session = _session()
    with patch("aiohttp.ClientSession", return_value=session) as client:
        await _backend().endpoints_for("gemma4:e4b")
    assert isinstance(client.call_args.kwargs["connector"]._resolver, _PinnedResolver)
    assert session.get.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize(
    "bad",
    [
        "64:ff9b::a9fe:a9fe",
        "2002:0a00:0001::",
        "2001:0:4136:e378:8000:63bf:f5ff:fffe",  # teredo server 65.54.227.120, client 10.0.0.1
        "2001:0:0a00:0001:8000:63bf:3fff:fdd2",  # teredo server 10.0.0.1
        "::ffff:127.0.0.1",
        "64:ff9b:1::a00:1",
        "224.0.0.1",
    ],
)
def test_embedded_ipv4_and_non_global_forms_forbidden(bad):
    """Verify: NAT64/6to4/Teredo/mapped wrappers around non-public IPv4 are forbidden."""
    assert _is_forbidden(ip_address(bad))


@pytest.mark.parametrize("good", ["104.18.7.1", "2606:4700::6812:701", "64:ff9b::6812:701"])
def test_public_forms_allowed(good):
    """Verify: genuinely public IPv4/IPv6 (incl. NAT64 of a public v4) stay allowed."""
    assert not _is_forbidden(ip_address(good))
