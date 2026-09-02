"""Live-Neo4j fixtures for the graph-platform integration harness (Task 14).

Mirrors ``tests/integration/conftest.py``'s ``ollama_available`` probe
pattern: fixtures here honestly ``pytest.skip`` (never fail, never silently
pass) when the live Neo4j container is unreachable, so `make
test-graph-integration` degrades gracefully without a container while still
proving isolation/round-trip behavior for real when `make graph-neo4j-up`
has been run.

Isolation approach: ``TenantGraphClient``/``Neo4jGraphStore`` deliberately
expose no unscoped "wipe everything" call (every method requires a
``TenantScope`` -- see ``shared/graph/client.py``'s module docstring), so
this harness does not attempt a full-graph reset between tests. Instead,
``unique_scope`` hands each test a fresh, randomly-suffixed
``TenantScope`` so concurrent/leftover state from another test or a prior
interrupted run can never collide with it, and tests are expected to
``delete_scope`` their own data in a ``try``/``finally`` so cleanup still
runs on assertion failure.

``graph_client`` is session-scoped (one real driver/session for the whole
`tests/integration/graph` run, not one per test) -- any test module that
uses it must set ``pytestmark = pytest.mark.asyncio(loop_scope="session")``
(see ``tests/unit/proxy/test_proxy_server_main.py`` for the established
in-repo convention) so pytest-asyncio binds it to the same event loop the
fixture was created on.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable

import pytest
import pytest_asyncio
from neo4j import AsyncGraphDatabase

from shared.graph.client import TenantGraphClient
from shared.graph.drivers.neo4j_driver import create_neo4j_store
from shared.graph.resolver import ResolvedInstance
from shared.graph.types import TenantScope

BOLT = os.getenv("WADDLEAI_GRAPH_BOLT_URL", "")
USER = os.getenv("WADDLEAI_GRAPH_USER", "neo4j")
PASSWORD = os.getenv("WADDLEAI_GRAPH_PASSWORD", "")
_PROBE_TIMEOUT_SECS = 5.0


async def _bolt_reachable(bolt_url: str, user: str, password: str) -> tuple[bool, str]:
    """Attempt a genuine bolt handshake + auth round-trip; return (ok, reason).

    A real ``AsyncDriver.verify_connectivity()`` call, not a bare TCP
    connect -- a raw socket can succeed against a container whose port is
    open but whose bolt listener isn't actually serving yet, which would
    make the skip dishonest (looks reachable, then every test in the file
    errors instead of cleanly skipping). Bounded by
    ``_PROBE_TIMEOUT_SECS`` so an unreachable host fails fast rather than
    hanging fixture setup for the whole session.
    """
    if not bolt_url:
        return False, "WADDLEAI_GRAPH_BOLT_URL is not set"
    driver = AsyncGraphDatabase.driver(
        bolt_url, auth=(user, password), connection_timeout=_PROBE_TIMEOUT_SECS
    )
    try:
        await asyncio.wait_for(driver.verify_connectivity(), timeout=_PROBE_TIMEOUT_SECS)
    except Exception as exc:  # noqa: BLE001 -- any failure means "unreachable"; report why, don't mask
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        await driver.close()
    return True, ""


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def graph_client() -> AsyncIterator[TenantGraphClient]:
    """A real ``TenantGraphClient`` bound to the live test Neo4j; skips the session if unreachable.

    Dev-mode short-circuit: every ``org_id`` resolves to the one shared
    ``WADDLEAI_GRAPH_BOLT_URL`` instance via an injected resolver, the same
    behavior ``shared.graph.resolver.resolve_or_dev`` provides in
    production dev-mode -- but without requiring a live Postgres
    ``graph_instances`` row, since this harness's scope is the graph
    backend only (Task 14). The ``store_factory``/``resolver`` injection
    points are exactly what production code leaves open for this
    (``shared/graph/client.py``); the client returned is otherwise the
    real, unmodified ``TenantGraphClient``, backed by the real
    ``Neo4jGraphStore``/Cypher compiler -- nothing here is a fake.
    """
    reachable, reason = await _bolt_reachable(BOLT, USER, PASSWORD)
    if not reachable:
        pytest.skip(
            f"Neo4j not reachable at {BOLT or '<unset WADDLEAI_GRAPH_BOLT_URL>'} -- {reason}. "
            "Run `make graph-neo4j-up` first."
        )

    async def _resolver(_db: object, _org_id: object) -> ResolvedInstance:
        """Return the one shared test instance, ignoring db/org_id (dev-mode semantics)."""
        return ResolvedInstance(bolt_url=BOLT, user=USER, password=PASSWORD)

    client = TenantGraphClient(
        db=object(),
        store_factory=lambda inst: create_neo4j_store(inst.bolt_url, inst.user, inst.password),
        resolver=_resolver,
    )
    yield client
    await client.aclose()


@pytest.fixture
def seed_ready_instance() -> bool:
    """No-op marker -- `graph_client`'s resolver always treats the shared test Neo4j as ready."""
    return True


@pytest.fixture
def unique_scope() -> Callable[[str | None], TenantScope]:
    """Return a factory building a `TenantScope` with a random org/repo suffix per call.

    Each call gets a fresh UUID4 suffix, so two tests (or two runs of the
    same test against a container that wasn't torn down) can never collide
    on the same node keys -- the isolation mechanism this harness relies on
    instead of an unscoped full-graph wipe (see module docstring).
    """

    def _factory(branch_ref: str | None = "main") -> TenantScope:
        suffix = uuid.uuid4().hex[:12]
        return TenantScope(
            org_id=f"itest-org-{suffix}", repo_id=f"itest-repo-{suffix}", branch_ref=branch_ref
        )

    return _factory
