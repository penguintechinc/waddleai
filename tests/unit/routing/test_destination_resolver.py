"""Tests for DestinationResolver (spec §5.2) -- S2/S8/S9 security invariants.

Covers: row mapping + timeout default, pin/local_only filtering, region
fallback to provider extra_config, TTL cache keyed by (org_id, model) with an
injectable clock, load_material's secret-bearing row, and (self-review and
review-round-1 hardening) a Python-side defense-in-depth guard that excludes
and logs any row whose credential fails S2 ownership OR the same-provider
match, even if it somehow reached this layer despite the SQL predicate.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest

from shared.routing.destinations import Destination, DestinationResolver

# One joined row shape (mirrors the SELECT column order in the impl):
# (id, organization_id, model, priority, provider_id, provider_type, endpoint_url,
#  provider_extra_config, provider_model_id, region, timeout_seconds, credential_id,
#  owner_org_id, updated_at, credential_provider_id)
_ROW_FIELDS = (
    "id",
    "organization_id",
    "model",
    "priority",
    "provider_id",
    "provider_type",
    "endpoint_url",
    "provider_extra_config",
    "provider_model_id",
    "region",
    "timeout_seconds",
    "credential_id",
    "owner_org_id",
    "updated_at",
    "credential_provider_id",
)


def _row(**kw: object) -> tuple:
    """Build one joined SQL row tuple from defaults, overridden by kw.

    ``credential_provider_id`` defaults to the same value as ``provider_id``
    (the credential belongs to the destination's own provider) so existing
    callers that don't care about the provider-match predicate stay valid.
    """
    base = dict(
        id=1,
        organization_id=7,
        model="claude-sonnet-4",
        priority=0,
        provider_id=3,
        provider_type="bedrock",
        endpoint_url=None,
        provider_extra_config=None,
        provider_model_id="anthropic.claude-sonnet-4-v1:0",
        region="us-west-2",
        timeout_seconds=None,
        credential_id=5,
        owner_org_id=7,
        updated_at="2026-09-04T00:00:00",
    )
    base.update(kw)
    base.setdefault("credential_provider_id", base["provider_id"])
    return tuple(base[k] for k in _ROW_FIELDS)


class _FakeDB:
    """Minimal executesql stand-in: records calls, returns fixed rows regardless of params."""

    def __init__(self, rows: list[tuple]) -> None:
        """Seed the fake table with the given joined rows."""
        self._rows = rows
        self.calls: list[tuple] = []

    def executesql(self, sql: str, params: list[object] | None = None) -> list[tuple]:
        """Record the call and return the seeded rows, ignoring the SQL/params."""
        self.calls.append((sql, params))
        return list(self._rows)


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
async def test_resolve_maps_rows_and_defaults_timeout() -> None:
    """resolve() maps a joined row to a Destination, defaulting a NULL timeout to 30."""
    db = _FakeDB([_row()])
    dests = await DestinationResolver(db).resolve(7, "claude-sonnet-4")
    assert len(dests) == 1
    d = dests[0]
    assert isinstance(d, Destination)
    assert d.timeout_seconds == 30  # NULL -> 30 default
    assert d.provider_type == "bedrock" and d.role == "active"
    assert d.credential_version == "2026-09-04T00:00:00"
    # org_id + model bound as params (S8 -- never interpolated)
    _sql, params = db.calls[0]
    assert 7 in params and "claude-sonnet-4" in params


@pytest.mark.asyncio
async def test_pin_keeps_only_matching_provider() -> None:
    """pin= filters the resolved list down to matching provider_type rows only."""
    db = _FakeDB(
        [
            _row(id=1, provider_type="bedrock", priority=0),
            _row(id=2, provider_type="ollama", priority=1),
        ]
    )
    dests = await DestinationResolver(db).resolve(7, "m", pin="ollama")
    assert [d.id for d in dests] == [2]


@pytest.mark.asyncio
async def test_local_only_keeps_only_local_providers() -> None:
    """local_only=True keeps only ollama/llamacpp destinations."""
    db = _FakeDB(
        [
            _row(id=1, provider_type="bedrock", priority=0),
            _row(id=2, provider_type="llamacpp", priority=1),
            _row(id=3, provider_type="ollama", priority=2),
        ]
    )
    dests = await DestinationResolver(db).resolve(7, "m", local_only=True)
    assert sorted(d.id for d in dests) == [2, 3]


@pytest.mark.asyncio
async def test_region_falls_back_to_provider_extra_config() -> None:
    """A NULL destination region falls back to the provider's extra_config region."""
    db = _FakeDB([_row(region=None, provider_extra_config='{"region": "eu-central-1"}')])
    dests = await DestinationResolver(db).resolve(7, "m")
    assert dests[0].region == "eu-central-1"


@pytest.mark.asyncio
async def test_ttl_cache_is_keyed_by_org_and_model() -> None:
    """The TTL cache is per (org_id, model); a different org always re-reads (S8)."""
    clock = _Clock()
    db = _FakeDB([_row()])
    r = DestinationResolver(db, ttl_seconds=30.0, clock=clock)
    await r.resolve(7, "m")
    await r.resolve(7, "m")
    assert len(db.calls) == 1  # cached within TTL
    await r.resolve(8, "m")  # different org -> new read (S8)
    assert len(db.calls) == 2
    clock.tick(31)
    await r.resolve(7, "m")
    assert len(db.calls) == 3  # TTL expired


@pytest.mark.asyncio
async def test_load_material_returns_secret_bearing_row() -> None:
    """load_material() returns the encrypted material, excluded from its own repr (S4)."""

    class _DB:
        def executesql(self, sql: str, params: list[object] | None = None) -> list[tuple]:
            return [(5, 3, 7, "enc:xxxxx", "2026-09-04T00:00:00")]

    mat = await DestinationResolver(_DB()).load_material(5)
    assert mat is not None
    assert mat.credential_id == 5 and mat.owner_org_id == 7
    assert mat.encrypted_material == "enc:xxxxx"
    assert "enc:xxxxx" not in repr(mat)  # S4 -- secret excluded from repr


# --- Self-review hardening: defense-in-depth beyond the SQL predicate -----


@pytest.mark.asyncio
async def test_cross_org_credential_excluded_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S2/S8 defense-in-depth against a row that bypasses the SQL WHERE predicate.

    Even if such a row reaches this layer with a credential owned by a
    *different* org than the destination's own org (a bug bypassing the SQL
    predicate, or a stub DB in tests that never evaluates SQL), resolve()
    must never hand that destination back to a caller -- it is excluded and
    logged as a config defect rather than silently used.
    """
    db = _FakeDB([_row(id=9, organization_id=7, owner_org_id=99)])
    with caplog.at_level(logging.ERROR, logger="shared.routing.destinations"):
        dests = await DestinationResolver(db).resolve(7, "claude-sonnet-4")
    assert dests == []
    assert any(
        "config defect" in record.message and "9" in record.message for record in caplog.records
    )


@pytest.mark.asyncio
async def test_credential_provider_mismatch_excluded_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S2 defense-in-depth: a same-org credential for a *different* provider is excluded.

    Ownership alone (same org) is not sufficient -- the credential must also
    belong to the same ai_provider as the destination it is attached to. A
    row that reaches this layer with a same-org credential whose
    ``provider_id`` differs from the destination's own ``provider_id`` (e.g.
    an OpenAI credential wired into a Bedrock destination) must be excluded
    and logged as a config defect (ids only, never credential material),
    exactly like an ownership mismatch.
    """
    db = _FakeDB(
        [_row(id=9, organization_id=7, provider_id=3, owner_org_id=7, credential_provider_id=99)]
    )
    with caplog.at_level(logging.ERROR, logger="shared.routing.destinations"):
        dests = await DestinationResolver(db).resolve(7, "claude-sonnet-4")
    assert dests == []
    assert any(
        "config defect" in record.message and "9" in record.message for record in caplog.records
    )


@pytest.mark.asyncio
async def test_platform_pool_credential_is_never_excluded() -> None:
    """owner_org_id NULL (platform pool / no credential) is always allowed through."""
    db = _FakeDB([_row(id=1, organization_id=7, owner_org_id=None, credential_id=None)])
    dests = await DestinationResolver(db).resolve(7, "claude-sonnet-4")
    assert [d.id for d in dests] == [1]


def test_destination_repr_carries_no_secret_material() -> None:
    """Destination has no field for credential material at all (S4).

    Locks the invariant structurally, not just by example, so a future field
    addition that accidentally carries secret material trips this test.
    """
    field_names = {f.name for f in dataclasses.fields(Destination)}
    assert not any(
        token in name for name in field_names for token in ("api_key", "secret", "material")
    )
    d = Destination(
        id=1,
        organization_id=7,
        model="m",
        priority=0,
        provider_id=3,
        provider_type="bedrock",
        endpoint_url=None,
        region=None,
        provider_model_id=None,
        timeout_seconds=30,
        credential_id=5,
        owner_org_id=7,
        credential_version="v1",
    )
    assert "enc:" not in repr(d)
