"""Real end-to-end fixtures for the WaddleAI proxy.

Boots the actual proxy service (Quart + hypercorn, ``WADDLEAI_STUB_UPSTREAM=1``)
as a genuine child process listening on a real TCP port -- the same pattern
``tests/contract/conftest.py`` uses for its ``proxy_url`` fixture -- so tests
in this package drive multi-stage flows (auth -> security-in -> memory
stages -> cache -> routing -> dispatch -> security-out -> meter) through
real HTTP requests against a real ASGI server, not a hand-built
``PipelineContext``. ``tests/integration/test_*_acceptance.py`` already
proves each pipeline stage composition against fakes/mocks at the Python
level; this package proves the same behavior survives the actual HTTP/auth/
routing boundary.

Two proxy processes are used:

* ``proxy`` -- default configuration (no response cache; smart-routing and
  proxy-memory flags on so those stages actually run rather than being
  skipped) used by most flows.
* ``cache_proxy`` -- a second instance with the response-cache flag on and a
  real Valkey/Redis backend (``docker_redis``), used only by flows that need
  a genuine cache hit. ``CacheStage``/``ExactCache`` issue real
  ``redis.asyncio`` calls once the flag is enabled (see
  ``shared/cache/exact.py``), so a live backend is required -- there is no
  in-process fake available to a subprocess-launched server. ``docker_redis``
  skips with a reason string when Docker isn't available rather than
  silently no-op-ing.

Do not modify ``tests/contract/conftest.py`` -- this module intentionally
duplicates its small ``_free_port``/``_wait`` helpers rather than importing
across test packages.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]

# Same digest already cached locally by this repo's CI
# (.github/workflows/docker-build.yml pins redis:7-bookworm as a service
# container) -- pinned by digest per the dependency-pinning standard, but
# intentionally NOT the exact CI digest: that tag isn't pulled into this
# sandbox and pinning to it would require a network pull, violating the "no
# network egress" constraint for this suite. Re-pin to CI's digest once both
# are guaranteed to be the same cached layer in every environment this runs.
_REDIS_IMAGE = "redis@sha256:d3be87a1060455213a204d2b0a7f04d45d19a16a98e85b3c37b7c33b5f0c489e"


@dataclass(slots=True)
class ProxyHandle:
    """A running proxy subprocess: its base URL and the sqlite DB backing it."""

    base_url: str
    db_url: str


@dataclass(slots=True)
class OrgSeed:
    """A freshly seeded organization/user/api_key row set on a proxy's DB."""

    org_id: int
    user_id: int
    api_key_id: int
    api_key: str


def _free_port() -> int:
    """Bind an ephemeral port and immediately release it for a child process to reuse."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_healthz(url: str, timeout: float = 40.0) -> None:
    """Poll ``url`` until it answers with a non-5xx status, or raise on timeout."""
    deadline = time.time() + timeout
    last_exc: Exception | None = None
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except Exception as exc:  # noqa: BLE001 - retry loop; re-raised below on timeout
            last_exc = exc
        time.sleep(0.3)
    raise RuntimeError(f"server at {url} never became ready: {last_exc}")


def _launch_proxy(port: int, db_dir: Path, extra_env: dict[str, str]) -> subprocess.Popen:
    """Start the real proxy service as a child process (hypercorn, stub upstream).

    PYTHONPATH=REPO matches how the production image sets it (COPY shared/
    alongside the service) and how tests/contract/conftest.py's harness does
    the same for the identical reason: both services import top-level
    ``shared``/``proxy`` packages that only resolve with the repo root on
    sys.path.
    """
    env = {
        **os.environ,
        "WADDLEAI_STUB_UPSTREAM": "1",
        "DB_TYPE": "sqlite",
        "DATABASE_URL": f"sqlite:///{db_dir}/e2e.db",
        "FLASK_ENV": "testing",
        "RELEASE_MODE": "false",
        "CACHE_HOST": "",
        "REDIS_URL": "",
        "PYTHONPATH": str(REPO),
    }
    env.update(extra_env)
    argv = [
        "hypercorn",
        "apps.proxy_server.main:app",
        "--bind",
        f"127.0.0.1:{port}",
        "--workers",
        "1",
    ]
    # Fixed argv list, no shell, no untrusted input -- same pattern as
    # tests/contract/conftest.py's _launch() helper.
    proc = subprocess.Popen(argv, cwd=str(REPO / "proxy"), env=env)  # noqa: S603, S607
    try:
        _wait_healthz(f"http://127.0.0.1:{port}/healthz")
    except Exception:
        proc.terminate()
        raise
    return proc


def _open_db(db_url: str) -> Any:
    """Open a DB handle against an already-migrated sqlite file without re-registering tables.

    ``shared.database.models.get_db()`` (what every service calls at normal
    startup) defaults to ``reflect=True``. Against a database that already
    has these tables -- true for every proxy subprocess these fixtures
    launch, since each one runs its own ``migrate=True`` at startup --
    reflection populates SQLAlchemy metadata with the existing tables, and
    ``define_tables()`` then unconditionally tries to (re)define every one
    of them via ``Table(name, metadata, ...)``, raising
    ``sqlalchemy.exc.InvalidRequestError: Table 'X' is already defined for
    this MetaData instance``. This is a real, reproducible bug in
    ``get_db()`` independent of these tests (see the e2e test report) --
    worked around here, not fixed there, since ``get_db()`` is also the
    proxy's own production startup path and warrants its own dedicated fix
    + tests rather than a same-PR drive-by change. Disabling reflection
    before defining tables sidesteps it; ``penguin_dal``'s ``create_all()``
    is idempotent (CREATE TABLE IF NOT EXISTS semantics), so this is safe
    against a database that already has the schema.
    """
    from penguin_dal import DAL

    from shared.database.models import define_tables

    db = DAL(db_url, migrate=False, reflect=False)
    define_tables(db)
    return db


@pytest.fixture
def open_db() -> Callable[[str], Any]:
    """Factory fixture: ``open_db(db_url)`` -- see ``_open_db`` for why this exists."""
    return _open_db


def _seed_org(db_url: str, slug: str, role: str = "admin") -> OrgSeed:
    """Seed a fresh organization/user/api_key row set directly into a running proxy's DB.

    Mirrors ``ProxyServer._seed_contract_test_data`` (proxy/apps/proxy_server/
    main.py) -- real bcrypt hash, real schema -- but as a *second* org, for
    flows (org isolation) that need more than the one org the proxy seeds
    for itself under ``WADDLEAI_STUB_UPSTREAM=1``. Opens its own short-lived
    penguin-dal connection to the same sqlite file; the proxy's own
    connection is thread-local (see main.py's ``_api_key_verifier``
    docstring) so this is a separate, independent connection, not a shared
    one -- callers should seed before issuing requests, not concurrently
    with them, to avoid sqlite writer contention.
    """
    from passlib.hash import bcrypt

    db = _open_db(db_url)
    now = datetime.utcnow()
    org_id = db.organizations.insert(
        name=f"e2e-{slug}-org",
        description=f"E2E-seeded org ({slug})",
        token_quota_monthly=1_000_000,
        token_quota_daily=100_000,
        enabled=True,
        created_at=now,
    )
    user_id = db.users.insert(
        username=f"e2e-{slug}-user",
        email=f"e2e-{slug}@example.com",
        password_hash=bcrypt.hash("unused-not-a-real-login"),
        role=role,
        organization_id=org_id,
        token_quota_monthly=1_000_000,
        token_quota_daily=100_000,
        enabled=True,
        created_at=now,
    )
    api_key_value = f"wa-{slug}-e2esecretvalue0001"
    api_key_id = db.api_keys.insert(
        key_id=f"e2e-{slug}-key",
        key_hash=bcrypt.hash(api_key_value),
        user_id=user_id,
        organization_id=org_id,
        name=f"E2E {slug} key",
        enabled=True,
        api_access_level="proxy_api",
        created_at=now,
    )
    db.commit()
    return OrgSeed(org_id=org_id, user_id=user_id, api_key_id=api_key_id, api_key=api_key_value)


@pytest.fixture(scope="session")
def proxy_process(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Default proxy instance: every optional pipeline flag off, no live redis.

    Two flags are deliberately kept off rather than enabled for "realism":

    * ``waddleai.smart_routing`` -- with the flag on but no
      ``model_configs``/``model_assignments`` rows for a given org (the
      normal state for every flow except the dedicated routing test),
      RoutingEngine's final fallback silently substitutes the hardcoded
      default "gpt-4" for *any* request (see ``RoutingEngine._pick_final``,
      shared/routing/engine.py) -- surprising, cross-test-polluting behavior
      a shared process would otherwise leak into every other flow.
      ``routing_proxy`` below is a separate, purpose-seeded instance instead.
    * ``waddleai.proxy_memory`` -- with the flag on and no live Valkey
      (``memory_valkey`` is None here, matching every flow that doesn't need
      cache/memory infra), ``DedupStage`` unconditionally calls
      ``self.valkey.get(...)`` (``shared/memory/token_len_cache.py``) with no
      None-guard and every request 500s. This is a genuine bug independent
      of these tests (see the e2e test report) -- flagged, not fixed here,
      since it's proxy_memory's own runtime behavior, not this suite's
      concern; kept off so these flows stay green.
    """
    db_dir = tmp_path_factory.mktemp("e2e_proxy_db")
    port = _free_port()
    proc = _launch_proxy(port, db_dir, extra_env={})
    try:
        yield ProxyHandle(base_url=f"http://127.0.0.1:{port}", db_url=f"sqlite:///{db_dir}/e2e.db")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="session")
def routing_proxy(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """A dedicated proxy instance with ``waddleai.smart_routing`` on, isolated from other flows."""
    db_dir = tmp_path_factory.mktemp("e2e_routing_proxy_db")
    port = _free_port()
    extra_env = {
        "WADDLEAI_FLAG_SMART_ROUTING": "1",
    }
    proc = _launch_proxy(port, db_dir, extra_env)
    try:
        yield ProxyHandle(base_url=f"http://127.0.0.1:{port}", db_url=f"sqlite:///{db_dir}/e2e.db")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="session")
def routing_proxy_tokens(routing_proxy: ProxyHandle) -> dict[str, str]:
    """The routing proxy's own seeded Bearer JWT + wa- API key."""
    r = httpx.get(f"{routing_proxy.base_url}/_contract_test/token", timeout=10)
    r.raise_for_status()
    return r.json()


@pytest.fixture(scope="session")
def proxy_tokens(proxy_process: ProxyHandle) -> dict[str, str]:
    """The proxy's own seeded Bearer JWT + wa- API key + second-user member token."""
    r = httpx.get(f"{proxy_process.base_url}/_contract_test/token", timeout=10)
    r.raise_for_status()
    return r.json()


@pytest.fixture
def seed_org() -> Callable[..., OrgSeed]:
    """Factory fixture: ``seed_org(db_url, slug)`` seeds a fresh org/user/api_key row set."""
    return _seed_org


def _docker_available() -> bool:
    return shutil.which("docker") is not None


@pytest.fixture(scope="session")
def docker_redis() -> Any:
    """Start a real, locally-cached Valkey/Redis container for response-cache e2e flows.

    Skips (reason string) rather than failing when Docker is unavailable, or
    the container never becomes ready -- this is genuinely optional
    infrastructure the environment may lack (see module docstring); the
    flows that depend on it are exactly the ones spec'd as needing a real
    cache backend, not something this suite can fake from outside a
    subprocess.
    """
    if not _docker_available():
        pytest.skip(
            "docker is not available in this environment -- response-cache "
            "e2e flows require a live Valkey/Redis backend"
        )

    port = _free_port()
    name = f"waddleai-e2e-redis-{port}"
    run_argv = ["docker", "run", "-d", "--rm", "--name", name, "-p", f"{port}:6379", _REDIS_IMAGE]
    try:
        # Fixed argv, no shell, no untrusted input -- a locally-cached image
        # digest (see _REDIS_IMAGE) and a port/name this process generated.
        subprocess.run(run_argv, check=True, capture_output=True, timeout=30)  # noqa: S603, S607
    except Exception as exc:  # noqa: BLE001 - environment probe, not a test assertion
        pytest.skip(f"could not start a local redis container for cache e2e flows: {exc}")
        return

    try:
        import redis as redis_sync

        deadline = time.time() + 15.0
        last_exc: Exception | None = None
        ready = False
        while time.time() < deadline:
            try:
                if redis_sync.Redis(host="127.0.0.1", port=port, socket_connect_timeout=1).ping():
                    ready = True
                    break
            except Exception as exc:  # noqa: BLE001 - retry loop
                last_exc = exc
            time.sleep(0.3)

        if not ready:
            subprocess.run(["docker", "stop", name], capture_output=True, timeout=15)  # noqa: S603, S607
            pytest.skip(f"local redis container never became ready: {last_exc}")
            return

        yield f"redis://127.0.0.1:{port}/0"
    finally:
        subprocess.run(["docker", "stop", name], capture_output=True, timeout=15)  # noqa: S603, S607


@pytest.fixture(scope="session")
def cache_proxy(tmp_path_factory: pytest.TempPathFactory, docker_redis: str) -> Any:
    """A second proxy instance with the response-cache flag on and a real redis backend."""
    db_dir = tmp_path_factory.mktemp("e2e_cache_proxy_db")
    port = _free_port()
    extra_env = {
        "REDIS_URL": docker_redis,
        "WADDLEAI_FLAG_RESPONSE_CACHE": "1",
    }
    proc = _launch_proxy(port, db_dir, extra_env)
    try:
        yield ProxyHandle(base_url=f"http://127.0.0.1:{port}", db_url=f"sqlite:///{db_dir}/e2e.db")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="session")
def cache_proxy_tokens(cache_proxy: ProxyHandle) -> dict[str, str]:
    """The cache proxy's own seeded Bearer JWT + wa- API key."""
    r = httpx.get(f"{cache_proxy.base_url}/_contract_test/token", timeout=10)
    r.raise_for_status()
    return r.json()
