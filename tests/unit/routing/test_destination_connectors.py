"""Tests for DestinationConnectorRegistry (spec §5.5) -- S2 build assert, S4 material.

Covers: OpenAI connector build with decrypted material passthrough, same-version
reuse, rotated-version rebuild, both S2 mismatch kinds (cross-org ownership and
cross-provider), the platform-pool (NULL owner) allow path, the ambient-AWS-chain
path (no credential_id), cache-key composition (never includes material or
request-specific fields like dest.id/model), bounded LRU + idle eviction with
best-effort close() release, and that decrypted material never appears in a log
line, a connector repr/str, or the registry's own repr.
"""

from __future__ import annotations

import logging

import pytest

from shared.routing.destination_connectors import DestinationConnectorRegistry, OwnershipError
from shared.routing.destinations import CredentialMaterial, Destination


def _dest(
    *,
    id: int = 1,
    organization_id: int = 7,
    model: str = "m",
    priority: int = 0,
    provider_id: int = 3,
    provider_type: str = "openai",
    endpoint_url: str | None = "http://127.0.0.1:9/v1",
    region: str | None = None,
    provider_model_id: str | None = None,
    timeout_seconds: int = 30,
    credential_id: int | None = 5,
    owner_org_id: int | None = 7,
    credential_version: str = "v1",
) -> Destination:
    """Build one Destination from defaults (provider_id=3, credential_id=5), kwargs override."""
    return Destination(
        id=id,
        organization_id=organization_id,
        model=model,
        priority=priority,
        provider_id=provider_id,
        provider_type=provider_type,
        endpoint_url=endpoint_url,
        region=region,
        provider_model_id=provider_model_id,
        timeout_seconds=timeout_seconds,
        credential_id=credential_id,
        owner_org_id=owner_org_id,
        credential_version=credential_version,
    )


def _loader(material_by_id: dict[int, CredentialMaterial]):
    """Build an async credential loader closing over a fixed id -> CredentialMaterial map."""

    async def load(cid: int) -> CredentialMaterial | None:
        return material_by_id.get(cid)

    return load


class _Clock:
    """Manually advanceable clock, injected in place of time.monotonic."""

    def __init__(self) -> None:
        """Start the clock at t=0."""
        self.t = 0.0

    def __call__(self) -> float:
        """Return the current simulated time."""
        return self.t

    def tick(self, dt: float) -> None:
        """Advance the simulated time by dt seconds."""
        self.t += dt


@pytest.mark.asyncio
async def test_builds_openai_connector_with_decrypted_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """get() builds the mapped connector class, passing decrypted material as api_key."""
    built = {}

    class _FakeOpenAI:
        def __init__(self, name: str, config: dict) -> None:
            built["config"] = config
            self.name = name

    import shared.routing.destination_connectors as mod

    monkeypatch.setitem(mod._CONNECTOR_CLASSES, "openai", _FakeOpenAI)
    reg = DestinationConnectorRegistry(
        _loader({5: CredentialMaterial(5, 3, 7, encrypted_material="plain-key")})
    )
    conn = await reg.get(_dest())
    assert isinstance(conn, _FakeOpenAI)
    assert built["config"]["api_key"] == "plain-key"  # decrypt passthrough for non-enc value
    assert built["config"]["endpoint_url"] == "http://127.0.0.1:9/v1"


@pytest.mark.asyncio
async def test_same_version_reuses_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two get() calls for the same destination reuse one built connector."""
    calls = {"n": 0}

    class _FakeOpenAI:
        def __init__(self, name: str, config: dict) -> None:
            calls["n"] += 1

    import shared.routing.destination_connectors as mod

    monkeypatch.setitem(mod._CONNECTOR_CLASSES, "openai", _FakeOpenAI)
    reg = DestinationConnectorRegistry(
        _loader({5: CredentialMaterial(5, 3, 7, encrypted_material="k")})
    )
    await reg.get(_dest())
    await reg.get(_dest())
    assert calls["n"] == 1  # reused


@pytest.mark.asyncio
async def test_rotated_version_rebuilds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new credential_version yields a new cache key, dropping the stale client."""
    calls = {"n": 0}

    class _FakeOpenAI:
        def __init__(self, name: str, config: dict) -> None:
            calls["n"] += 1

    import shared.routing.destination_connectors as mod

    monkeypatch.setitem(mod._CONNECTOR_CLASSES, "openai", _FakeOpenAI)
    reg = DestinationConnectorRegistry(
        _loader({5: CredentialMaterial(5, 3, 7, encrypted_material="k")})
    )
    await reg.get(_dest(credential_version="v1"))
    await reg.get(_dest(credential_version="v2"))
    assert calls["n"] == 2  # new version -> new client


@pytest.mark.asyncio
async def test_ownership_mismatch_raises() -> None:
    """S2 #3: a credential owned by a different org than the destination is rejected."""
    reg = DestinationConnectorRegistry(
        _loader({5: CredentialMaterial(5, 3, 99, encrypted_material="k")})
    )
    with pytest.raises(OwnershipError):
        await reg.get(_dest(organization_id=7, owner_org_id=99))


@pytest.mark.asyncio
async def test_credential_provider_mismatch_raises() -> None:
    """S2 #3: a same-org credential attached to a *different* ai_provider is rejected.

    Ownership alone (matching org) is not sufficient -- the loaded
    credential's own provider_id must also match the destination's
    provider_id, exactly like DestinationResolver's same-provider guard.
    """
    reg = DestinationConnectorRegistry(
        _loader(
            {5: CredentialMaterial(5, 4, 7, encrypted_material="k")}
        )  # credential belongs to provider 4
    )
    with pytest.raises(OwnershipError):
        await reg.get(_dest(provider_id=3, credential_id=5, organization_id=7, owner_org_id=7))


@pytest.mark.asyncio
async def test_missing_credential_raises_ownership_error() -> None:
    """A credential_id the loader can't find is treated as an S2 build failure, not a crash."""
    reg = DestinationConnectorRegistry(_loader({}))
    with pytest.raises(OwnershipError):
        await reg.get(_dest())


@pytest.mark.asyncio
async def test_unsupported_provider_type_raises() -> None:
    """A destination naming a provider_type with no connector class is rejected, never built."""
    reg = DestinationConnectorRegistry(
        _loader({5: CredentialMaterial(5, 3, 7, encrypted_material="k")})
    )
    with pytest.raises(OwnershipError):
        await reg.get(_dest(provider_type="not-a-real-provider"))


@pytest.mark.asyncio
async def test_platform_credential_null_owner_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """owner_org_id NULL (platform pool) is always allowed through ownership."""

    class _FakeOpenAI:
        def __init__(self, name: str, config: dict) -> None:
            pass

    import shared.routing.destination_connectors as mod

    monkeypatch.setitem(mod._CONNECTOR_CLASSES, "openai", _FakeOpenAI)
    reg = DestinationConnectorRegistry(
        _loader({5: CredentialMaterial(5, 3, None, encrypted_material="k")})
    )
    conn = await reg.get(_dest(owner_org_id=None))
    assert conn is not None


@pytest.mark.asyncio
async def test_null_credential_id_builds_ambient(monkeypatch: pytest.MonkeyPatch) -> None:
    """No credential_id -> no loader call, api_key empty -> connector uses the ambient chain."""
    built = {}

    class _FakeBedrock:
        def __init__(self, name: str, config: dict) -> None:
            built["config"] = config

    import shared.routing.destination_connectors as mod

    monkeypatch.setitem(mod._CONNECTOR_CLASSES, "bedrock", _FakeBedrock)
    reg = DestinationConnectorRegistry(_loader({}))  # no material needed
    await reg.get(
        _dest(provider_type="bedrock", credential_id=None, owner_org_id=None, region="us-east-2")
    )
    assert built["config"]["api_key"] in ("", None)  # ambient chain
    assert built["config"]["aws_region"] == "us-east-2"


@pytest.mark.asyncio
async def test_anthropic_endpoint_url_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """The destination's endpoint_url reaches the Anthropic connector's config verbatim."""
    built = {}

    class _FakeAnthropic:
        def __init__(self, name: str, config: dict) -> None:
            built["config"] = config

    import shared.routing.destination_connectors as mod

    monkeypatch.setitem(mod._CONNECTOR_CLASSES, "anthropic", _FakeAnthropic)
    reg = DestinationConnectorRegistry(
        _loader({5: CredentialMaterial(5, 3, 7, encrypted_material="k")})
    )
    await reg.get(_dest(provider_type="anthropic", endpoint_url="https://proxy.example.com/v1"))
    assert built["config"]["endpoint_url"] == "https://proxy.example.com/v1"


def test_cache_key_excludes_request_specific_fields_and_material() -> None:
    """The cache key is (provider_id, credential_id, credential_version, endpoint_url, region) only.

    Two destinations differing only in id/model/timeout share one key --
    those fields don't distinguish which underlying connector is needed.
    """
    d1 = _dest(id=1, model="model-a", timeout_seconds=10)
    d2 = _dest(id=2, model="model-b", timeout_seconds=99)
    assert DestinationConnectorRegistry._key(d1) == DestinationConnectorRegistry._key(d2)
    assert DestinationConnectorRegistry._key(d1) == (3, 5, "v1", "http://127.0.0.1:9/v1", None)


@pytest.mark.asyncio
async def test_idle_eviction_rebuilds_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connector idle past idle_seconds is dropped, closed if possible, and rebuilt."""
    calls = {"n": 0}
    closed = []

    class _FakeOpenAI:
        def __init__(self, name: str, config: dict) -> None:
            self.name = name
            calls["n"] += 1

        async def close(self) -> None:
            closed.append(self.name)

    import shared.routing.destination_connectors as mod

    monkeypatch.setitem(mod._CONNECTOR_CLASSES, "openai", _FakeOpenAI)
    clock = _Clock()
    reg = DestinationConnectorRegistry(
        _loader({5: CredentialMaterial(5, 3, 7, encrypted_material="k")}),
        idle_seconds=900.0,
        clock=clock,
    )
    d = _dest()
    await reg.get(d)
    clock.tick(901.0)
    await reg.get(d)  # idle timeout exceeded -> old client closed, new one built
    assert calls["n"] == 2
    assert closed == ["dest:1"]


@pytest.mark.asyncio
async def test_eviction_without_close_method_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evicting a connector whose class has no close() (the base LLMConnector default) is safe."""

    class _FakeOpenAI:  # no close() defined -- most connector classes
        def __init__(self, name: str, config: dict) -> None:
            pass

    import shared.routing.destination_connectors as mod

    monkeypatch.setitem(mod._CONNECTOR_CLASSES, "openai", _FakeOpenAI)
    clock = _Clock()
    reg = DestinationConnectorRegistry(
        _loader({5: CredentialMaterial(5, 3, 7, encrypted_material="k")}),
        idle_seconds=1.0,
        clock=clock,
    )
    await reg.get(_dest())
    clock.tick(2.0)
    await reg.get(_dest())  # must not raise despite the evicted connector lacking close()


@pytest.mark.asyncio
async def test_close_failure_during_eviction_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A raising close() during eviction is logged (ids only) and never propagates to the caller."""

    class _FakeOpenAI:
        def __init__(self, name: str, config: dict) -> None:
            self.name = name

        async def close(self) -> None:
            raise RuntimeError("boom")

    import shared.routing.destination_connectors as mod

    monkeypatch.setitem(mod._CONNECTOR_CLASSES, "openai", _FakeOpenAI)
    clock = _Clock()
    reg = DestinationConnectorRegistry(
        _loader({5: CredentialMaterial(5, 3, 7, encrypted_material="k")}),
        idle_seconds=1.0,
        clock=clock,
    )
    await reg.get(_dest())
    clock.tick(2.0)
    with caplog.at_level(logging.WARNING):
        await reg.get(_dest())  # eviction's close() raises -- must not propagate
    assert any("dest:1" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_lru_capacity_evicts_oldest_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exceeding max_size drops the least-recently-used entry and best-effort closes it."""
    calls = {"n": 0}
    closed = []

    class _FakeOpenAI:
        def __init__(self, name: str, config: dict) -> None:
            self.name = name
            calls["n"] += 1

        async def close(self) -> None:
            closed.append(self.name)

    import shared.routing.destination_connectors as mod

    monkeypatch.setitem(mod._CONNECTOR_CLASSES, "openai", _FakeOpenAI)
    reg = DestinationConnectorRegistry(
        _loader({5: CredentialMaterial(5, 3, 7, encrypted_material="k")}), max_size=2
    )
    d1 = _dest(id=1, credential_version="v1")
    d2 = _dest(id=2, credential_version="v2")
    d3 = _dest(id=3, credential_version="v3")
    await reg.get(d1)
    await reg.get(d2)
    await reg.get(d3)  # exceeds max_size=2 -> evicts the LRU entry (d1) and closes it
    assert calls["n"] == 3
    assert closed == ["dest:1"]

    await reg.get(d1)  # d1's client was evicted -> rebuilt
    assert calls["n"] == 4


@pytest.mark.asyncio
async def test_material_never_logged_at_debug(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Decrypted (and encrypted) credential material never appears in a log line, even at DEBUG."""

    class _FakeOpenAI:
        def __init__(self, name: str, config: dict) -> None:
            pass

    import shared.routing.destination_connectors as mod

    monkeypatch.setitem(mod._CONNECTOR_CLASSES, "openai", _FakeOpenAI)
    reg = DestinationConnectorRegistry(
        _loader({5: CredentialMaterial(5, 3, 7, encrypted_material="super-secret-value")})
    )
    with caplog.at_level(logging.DEBUG):
        await reg.get(_dest())
    assert "super-secret-value" not in caplog.text


@pytest.mark.asyncio
async def test_connector_and_registry_repr_never_leak_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """repr()/str() of the built connector and of the registry itself never surface material."""

    class _FakeOpenAI:
        def __init__(self, name: str, config: dict) -> None:
            self.name = name
            self._config = config  # test-only stash to prove default repr still hides it

    import shared.routing.destination_connectors as mod

    monkeypatch.setitem(mod._CONNECTOR_CLASSES, "openai", _FakeOpenAI)
    reg = DestinationConnectorRegistry(
        _loader({5: CredentialMaterial(5, 3, 7, encrypted_material="top-secret-value")})
    )
    conn = await reg.get(_dest())
    assert "top-secret-value" not in repr(conn)
    assert "top-secret-value" not in str(conn)
    assert "top-secret-value" not in repr(reg)
