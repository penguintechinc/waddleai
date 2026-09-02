"""Live-Neo4j proof that an unavailable/non-ready graph backend fails fast, never hangs.

(spec Section 8c)

Two distinct unavailability shapes, both consumer-facing as
`GraphUnavailableError` (the type `services/management/app/api/v1/graph.py`
and the MCP graph adapter map to a clean 503/empty result -- see those
modules' docstrings):

1. A `graph_instances` row that exists but isn't `status='ready'` (or is
   missing entirely) -- `resolve_instance` (`shared/graph/resolver.py`) must
   reject it before ever attempting a Neo4j connection. This module runs
   under `make graph-neo4j-up` so a real, reachable Neo4j genuinely exists
   in the environment while this proof runs -- demonstrating the rejection
   is a DB-row decision, not an accidental side effect of no server being
   reachable at all.
2. A `ResolvedInstance` that resolves cleanly but points at an unreachable
   `bolt_url` (dead port, no listener) -- `create_neo4j_store` +
   `TenantGraphClient` must surface a clean failure within the driver's own
   bounded connection/acquisition timeouts (`create_neo4j_store`), not hang
   the caller. Proven with a real `asyncio.wait_for` cap around the call:
   the whole point of "never a hang" is a bounded-time assertion, not "it
   raises eventually".

Also closes a carry-forward from Task 8's review: a malformed/non-numeric
`org_id` reaching `resolve_instance` must fail closed as
`GraphUnavailableError`, never leak a raw DB exception -- see
`tests/unit/graph/test_resolver.py::test_bad_org_id_db_error_is_unavailable_not_raw`
for the fast unit-level proof (this module adds the same assertion here
for completeness, still without needing live Postgres -- see the module's
own `RaisingDB` fake below).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from shared.graph.client import TenantGraphClient
from shared.graph.drivers.neo4j_driver import create_neo4j_store
from shared.graph.resolver import ResolvedInstance, resolve_instance
from shared.graph.types import GraphQuery, GraphUnavailableError, TenantScope

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

# Generous but real caps -- proving "never a hang" means the test itself
# must be able to fail (time out) if the code under test regresses into
# actually hanging. A non-ready-row lookup never touches the network at
# all (it's a single fake `db.executesql` call), so its cap is tight; the
# unreachable-bolt case has to clear the real driver's own
# connection/connection_acquisition timeouts (5s / 10s, `create_neo4j_store`)
# plus TCP RST/refusal overhead, so its cap is looser.
_NON_READY_CAP_SECS = 2.0
_UNREACHABLE_BOLT_CAP_SECS = 20.0


class _NonReadyDB:
    """A db stand-in returning one fixed, non-ready-or-missing `graph_instances` row.

    Mirrors `tests/unit/graph/test_resolver.py`'s `FakeDB` -- kept local
    (rather than imported from the unit test module) so this integration
    module has no import-time dependency on `tests/unit`.
    """

    def __init__(self, row: tuple[str, str | None] | None) -> None:
        """Seed the single row a SELECT should return (None means no row)."""
        self._row = row

    def executesql(
        self, sql: str, params: list[object] | None = None
    ) -> list[tuple[str, str | None]]:
        """Return the seeded row (or none) regardless of the query text."""
        return [self._row] if self._row is not None else []

    def commit(self) -> None:
        """No-op -- this fake never writes."""


class _RaisingDB:
    """A db stand-in whose `executesql` raises, simulating a real driver's type error.

    Models what a real Postgres `org_id` integer column does when handed a
    non-numeric value -- see `shared/graph/client.py`'s module docstring
    for why `TenantGraphClient` can hand `resolve_instance` a non-numeric
    `scope.org_id` unfiltered.
    """

    def executesql(self, sql: str, params: list[object] | None = None) -> list[tuple[str, str]]:
        """Always raise -- the DB driver rejected the params before returning any row."""
        raise ValueError('invalid input syntax for type integer: "not-a-number"')

    def commit(self) -> None:
        """No-op -- never reached."""


@pytest.mark.parametrize(
    "row",
    [
        None,  # no graph_instances row at all
        ("pending", None),
        ("provisioning", None),
        ("failed", "bolt://neo4j:7687"),
        ("deprovisioning", "bolt://neo4j:7687"),
        ("deprovisioned", None),
        ("ready", None),  # ready but bolt_url never got populated
    ],
)
async def test_non_ready_row_is_unavailable_fast(row: tuple[str, str | None] | None) -> None:
    """Every non-ready status (or a missing row) raises `GraphUnavailableError`, fast.

    Runs while a real Neo4j is reachable at `WADDLEAI_GRAPH_BOLT_URL` (this
    whole module is invoked under `make graph-neo4j-up`) so the fast
    rejection is demonstrably a DB-row decision -- `resolve_instance` never
    imports or touches the `neo4j` driver at all, so a genuinely-live
    server changes nothing about this path; the cap catches a future
    regression that accidentally routes a non-ready row through a network
    call.
    """
    start = time.monotonic()
    with pytest.raises(GraphUnavailableError):
        await resolve_instance(_NonReadyDB(row), org_id=1)
    elapsed = time.monotonic() - start
    assert elapsed < _NON_READY_CAP_SECS, (
        f"non-ready lookup took {elapsed:.3f}s -- not a hang, but not fast either"
    )


async def test_bad_org_id_fails_closed_not_raw_exception() -> None:
    """A malformed org_id that trips the DB driver's own type check fails closed.

    Carry-forward from Task 8's review, proven here without live Postgres
    (a real integer-column type-check failure can't be produced against
    this harness's Neo4j-only fixtures) via a fake that raises exactly the
    way a real driver would on a non-numeric value against an integer
    column. `resolve_instance` must translate this into
    `GraphUnavailableError`, not let the raw `ValueError` propagate.
    """
    with pytest.raises(GraphUnavailableError) as exc_info:
        await resolve_instance(_RaisingDB(), org_id="not-a-number")  # type: ignore[arg-type]
    assert isinstance(exc_info.value.__cause__, ValueError)


async def test_unreachable_bolt_fails_within_bounded_timeout() -> None:
    """A dead bolt_url surfaces a clean failure within a bounded timeout -- never a hang.

    Port 9 is `discard` (RFC 863) if anything, but nothing binds a Neo4j
    bolt listener there -- the OS refuses the TCP connection immediately,
    so this exercises the "connection refused" fail-fast path. The
    `asyncio.wait_for` wrapper is the actual "never a hang" proof: if
    `create_neo4j_store`'s driver-level timeouts (`shared/graph/drivers/
    neo4j_driver.py`) were ever removed or misconfigured, this test would
    time out and fail loudly rather than hang the whole suite.
    """

    async def _dead_resolver(_db: object, _org_id: object) -> ResolvedInstance:
        return ResolvedInstance(bolt_url="bolt://127.0.0.1:9", user="neo4j", password="x")  # noqa: S106 -- test fixture, not a real credential

    client = TenantGraphClient(
        db=object(),
        store_factory=lambda inst: create_neo4j_store(inst.bolt_url, inst.user, inst.password),
        resolver=_dead_resolver,
    )
    scope = TenantScope(org_id="1", repo_id="1", branch_ref="main")

    # Deliberately NOT `pytest.raises(Exception)` wrapping `wait_for` directly --
    # `TimeoutError` (what a genuine hang produces) IS an `Exception`, so that
    # would silently treat a hang as "the expected failure" and pass -- exactly
    # the gate-that-cannot-fail bug this test exists to avoid. `TimeoutError`
    # is caught and explicitly failed; any other exception is the expected
    # driver-connectivity failure.
    start = time.monotonic()
    connectivity_error: BaseException | None = None
    try:
        await asyncio.wait_for(
            client.query(scope, GraphQuery(labels=("Class",))),
            timeout=_UNREACHABLE_BOLT_CAP_SECS,
        )
    except TimeoutError:
        elapsed = time.monotonic() - start
        pytest.fail(
            f"unreachable-bolt query hung past the {_UNREACHABLE_BOLT_CAP_SECS}s bound "
            f"(elapsed {elapsed:.3f}s) -- this IS the failure this test exists to catch"
        )
    except Exception as exc:  # noqa: BLE001 -- any non-timeout failure is the expected driver-connectivity error
        connectivity_error = exc

    elapsed = time.monotonic() - start
    assert connectivity_error is not None, (
        "expected a connectivity failure against a dead bolt port, got none -- "
        "port 9 unexpectedly accepted a bolt handshake?"
    )
    assert elapsed < _UNREACHABLE_BOLT_CAP_SECS, (
        f"unreachable-bolt query took {elapsed:.3f}s -- did not fail fast"
    )
    await client.aclose()
