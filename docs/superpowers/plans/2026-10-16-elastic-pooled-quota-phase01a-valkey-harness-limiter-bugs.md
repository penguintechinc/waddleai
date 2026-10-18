# Elastic Pooled Quota — Phase 1a: Real-Valkey Harness + TokenLimiter Bug A/B

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Master plan (header, Global Constraints, Review Focus): `2026-10-16-elastic-pooled-quota.md`. Spec: `docs/superpowers/specs/2026-10-14-elastic-pooled-quota-design.md` §6 (Bug A/B), §10.

Work in `/home/penguin/code/waddleai/.worktrees/elastic-pooled-quota`. Run python via `make`'s venv: `python3 -m pytest ...` when `.venv` exists.

---

### Task 1: Real-Valkey test harness

The repo has no fakeredis; every Lua test in this feature needs a real Valkey. Mirrors how the teams tests stood up real Postgres (`tests/integration/test_teams_migration_pg.py`).

**Files:**
- Create: `tests/integration/valkey_harness.py`
- Create: `tests/integration/quota/__init__.py` (empty)
- Create: `tests/integration/quota/conftest.py`
- Create: `tests/integration/quota/test_valkey_harness.py`
- Modify: `Makefile` (add `test-valkey` target + `.PHONY`)

**Interfaces:**
- Consumes: `tests.e2e.conftest._free_port() -> int`.
- Produces (fixtures, all in `tests/integration/quota/conftest.py`): `valkey_server -> ValkeyServer` (session), `valkey_async -> redis.asyncio.Redis` (function; `decode_responses=True`, flushed before each test), `valkey_sync -> redis.Redis` (function; same). `ValkeyServer(url: str, port: int)`. Env `REQUIRE_VALKEY_TESTS=1` turns "docker/Valkey unavailable" from skip into FAIL. Make target `test-valkey`.

- [ ] **Step 1: Write the failing test**

`tests/integration/quota/test_valkey_harness.py`:

```python
"""The real-Valkey harness itself: it must connect, isolate tests, and fail loudly when required."""

import pytest

from tests.integration import valkey_harness


async def test_async_client_round_trip(valkey_async):
    """A SET/GET and a Lua EVAL both work against the real server."""
    await valkey_async.set("k", "v")
    assert await valkey_async.get("k") == "v"
    assert await valkey_async.eval("return redis.call('GET', KEYS[1])", 1, "k") == "v"


async def test_database_is_flushed_between_tests(valkey_async):
    """The previous test's key must be gone (function-scoped flush)."""
    assert await valkey_async.get("k") is None


def test_sync_client_sees_same_server(valkey_sync):
    """The sync client is wired to the same server and also starts empty."""
    assert valkey_sync.ping() is True
    assert valkey_sync.dbsize() == 0


def test_required_env_turns_skip_into_failure(monkeypatch):
    """With REQUIRE_VALKEY_TESTS=1 an unavailable server is a hard failure, never a skip."""
    monkeypatch.setenv(valkey_harness.REQUIRE_ENV, "1")
    with pytest.raises(pytest.fail.Exception):
        valkey_harness.unavailable("no docker here")


def test_unavailable_skips_when_not_required(monkeypatch):
    """Without the env var the same condition is a skip (local dev without Docker)."""
    monkeypatch.delenv(valkey_harness.REQUIRE_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception):
        valkey_harness.unavailable("no docker here")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/integration/quota/test_valkey_harness.py -v --no-cov`
Expected: FAIL / ERROR with `ModuleNotFoundError: tests.integration.valkey_harness` (or fixture `valkey_async` not found).

- [ ] **Step 3: Write minimal implementation**

`tests/integration/valkey_harness.py`:

```python
"""Real-Valkey test harness: one Docker container per session, no fakeredis.

The Lua lease/reserve/reconcile scripts can only be proven against a real server (fakeredis
does not implement our scripts' semantics faithfully). Skips when Docker is missing unless
``REQUIRE_VALKEY_TESTS=1`` (set by ``make test-valkey`` and CI), in which case it FAILS --
a silently-skipped gate is the same defect as ``|| true`` (critical-rules Verification Integrity).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
import redis

from tests.e2e.conftest import _free_port

#: Pinned by tag here; Task 1 step 5 pins the digest via WADDLEAI_TEST_VALKEY_IMAGE in CI/Makefile.
VALKEY_IMAGE = os.environ.get("WADDLEAI_TEST_VALKEY_IMAGE", "valkey/valkey:8.0.1-bookworm")
REQUIRE_ENV = "REQUIRE_VALKEY_TESTS"


@dataclass(frozen=True)
class ValkeyServer:
    """Connection details of the session's Valkey container."""

    url: str
    port: int


def unavailable(reason: str) -> None:
    """Fail when Valkey tests are required, otherwise skip -- never silently pass."""
    if os.environ.get(REQUIRE_ENV) == "1":
        pytest.fail(f"{REQUIRE_ENV}=1 but {reason}", pytrace=False)
    pytest.skip(reason)


def _wait_ready(url: str, timeout_s: float = 30.0) -> bool:
    """Poll PING until the server answers or the timeout elapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            client = redis.Redis.from_url(url)
            try:
                if client.ping():
                    return True
            finally:
                client.close()
        except redis.RedisError:
            time.sleep(0.2)
    return False


def start_valkey() -> Iterator[ValkeyServer]:
    """Start a throwaway Valkey container, yield its address, always remove it."""
    if shutil.which("docker") is None:
        unavailable("docker is not available -- quota Lua tests need a real Valkey")
    port = _free_port()
    name = f"waddleai-quota-valkey-{uuid.uuid4().hex[:8]}"
    argv = ["docker", "run", "--rm", "-d", "--name", name, "-p", f"127.0.0.1:{port}:6379", VALKEY_IMAGE]
    try:
        subprocess.run(argv, check=True, capture_output=True, timeout=120, text=True)  # noqa: S603
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        unavailable(f"could not start {VALKEY_IMAGE}: {exc}")
    url = f"redis://127.0.0.1:{port}/0"
    try:
        if not _wait_ready(url):
            unavailable(f"{VALKEY_IMAGE} did not answer PING within 30s")
        yield ValkeyServer(url=url, port=port)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)  # noqa: S603,S607
```

`tests/integration/quota/conftest.py`:

```python
"""Fixtures exposing the real-Valkey harness to the quota integration tests."""

from collections.abc import AsyncIterator, Iterator

import pytest
import redis
import redis.asyncio as aredis

from tests.integration.valkey_harness import ValkeyServer, start_valkey


@pytest.fixture(scope="session")
def valkey_server() -> Iterator[ValkeyServer]:
    """Session-lived Valkey container (skip/fail policy lives in the harness)."""
    yield from start_valkey()


@pytest.fixture
async def valkey_async(valkey_server: ValkeyServer) -> AsyncIterator[aredis.Redis]:
    """Async client (decode_responses) on a freshly flushed database."""
    client = aredis.from_url(valkey_server.url, decode_responses=True)
    await client.flushall()
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def valkey_sync(valkey_server: ValkeyServer) -> Iterator[redis.Redis]:
    """Sync client (decode_responses) on a freshly flushed database."""
    client = redis.Redis.from_url(valkey_server.url, decode_responses=True)
    client.flushall()
    try:
        yield client
    finally:
        client.close()
```

`Makefile` — append `test-valkey` to the `.PHONY` list on line 1 and add after the `test-integration` recipe:

```make
test-valkey:
	@echo "Running real-Valkey quota tests (REQUIRE_VALKEY_TESTS=1: skip == fail)..."
	REQUIRE_VALKEY_TESTS=1 $(PY) -m pytest tests/integration/quota -v --no-cov
```

- [ ] **Step 4: Run test to verify it passes**

Run: `make test-valkey`
Expected: PASS (5 passed). If Docker is unavailable the run FAILS (that is correct under `REQUIRE_VALKEY_TESTS=1`); use `python3 -m pytest tests/integration/quota/test_valkey_harness.py --no-cov` to see the skip behaviour.

- [ ] **Step 5: Pin the image digest, then commit**

Run: `docker pull valkey/valkey:8.0.1-bookworm && docker inspect --format '{{index .RepoDigests 0}}' valkey/valkey:8.0.1-bookworm`
Edit `VALKEY_IMAGE`'s default in `tests/integration/valkey_harness.py` to the printed `valkey/valkey@sha256:<digest>` value (use that exact string; keep the tag in a trailing comment).

```bash
git add tests/integration/valkey_harness.py tests/integration/quota/__init__.py tests/integration/quota/conftest.py tests/integration/quota/test_valkey_harness.py Makefile
git commit -m "test(quota): real-Valkey harness + make test-valkey (skip==fail when required)"
git push
```

---

### Task 2: TokenLimiter Bug A — denied request must not leak counters

Spec §6 Bug A: `LUA_RESERVE` INCRBYs the TPM and token counters before the later budget checks run, so a request denied by the monthly-token or USD check leaves the earlier counters inflated.

**Files:**
- Modify: `shared/utils/token_limiter.py:49-100` (`LUA_RESERVE`)
- Create: `tests/integration/quota/test_limiter_regressions.py`

**Interfaces:**
- Consumes: `valkey_async` fixture (Task 1); `TokenLimiter(valkey, features)`, `KeyLimits(tpm_limit, monthly_token_limit, monthly_usd_limit)`, `GateDecision` (existing, `shared/utils/token_limiter.py`).
- Produces: a two-phase `LUA_RESERVE` (all checks read-only first, then all mutations); return shape `{1, "", reservation_id}` on allow and `{0, reason, ""}` on deny (no `cjson.null`, which truncates/behaves oddly across clients). `TokenLimiter.reserve` keeps its signature.

- [ ] **Step 1: Write the failing test**

`tests/integration/quota/test_limiter_regressions.py`:

```python
"""Regressions for the two TokenLimiter bugs (spec §6), against a real Valkey."""

from shared.utils.token_limiter import KeyLimits, TokenLimiter


class _FlagOn:
    """Feature helper stub: every flag on."""

    def is_feature_enabled(self, flag, **kwargs):
        return True


async def _keys(client, pattern):
    """All keys matching ``pattern`` (SCAN, never KEYS)."""
    return sorted([k async for k in client.scan_iter(match=pattern)])


async def test_regression_bug_a_denied_by_token_budget_leaks_no_tpm_counter(valkey_async):
    """TPM passes (80<=100) but the monthly-token budget denies (80>50): no counter may remain."""
    limiter = TokenLimiter(valkey_async, _FlagOn())
    decision = await limiter.reserve(
        7, 80, 0.0, KeyLimits(tpm_limit=100, monthly_token_limit=50, monthly_usd_limit=None)
    )
    assert decision.allowed is False
    assert decision.reason == "monthly_tokens_exceeded"
    assert await _keys(valkey_async, "waddleai:tpm:7:*") == []
    assert await _keys(valkey_async, "waddleai:budget:tok:7:*") == []
    assert await _keys(valkey_async, "waddleai:resv:*") == []


async def test_regression_bug_a_denied_by_usd_leaks_neither_tpm_nor_token_counter(valkey_async):
    """USD denial must leave both earlier counters untouched."""
    limiter = TokenLimiter(valkey_async, _FlagOn())
    decision = await limiter.reserve(
        8, 10, 5.0, KeyLimits(tpm_limit=100, monthly_token_limit=1000, monthly_usd_limit=1)
    )
    assert decision.allowed is False
    assert decision.reason == "monthly_usd_exceeded"
    assert await _keys(valkey_async, "waddleai:tpm:8:*") == []
    assert await _keys(valkey_async, "waddleai:budget:tok:8:*") == []
    assert await _keys(valkey_async, "waddleai:budget:usd:8:*") == []


async def test_allowed_request_still_increments_all_counters(valkey_async):
    """The fix must not break the happy path: an allowed reserve debits every counter."""
    limiter = TokenLimiter(valkey_async, _FlagOn())
    decision = await limiter.reserve(
        9, 40, 0.5, KeyLimits(tpm_limit=100, monthly_token_limit=1000, monthly_usd_limit=10)
    )
    assert decision.allowed is True
    assert decision.reservation_id
    (tpm_key,) = await _keys(valkey_async, "waddleai:tpm:9:*")
    (tok_key,) = await _keys(valkey_async, "waddleai:budget:tok:9:*")
    assert int(await valkey_async.get(tpm_key)) == 40
    assert int(await valkey_async.get(tok_key)) == 40
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/integration/quota/test_limiter_regressions.py -v --no-cov`
Expected: the two `bug_a` tests FAIL (`assert [...'waddleai:tpm:7:...'] == []`); the happy-path test passes.

- [ ] **Step 3: Write minimal implementation**

Replace the entire `LUA_RESERVE` string in `shared/utils/token_limiter.py` with:

```python
    LUA_RESERVE = """
    local vkey_id = ARGV[1]
    local estimated_tokens = tonumber(ARGV[2])
    local estimated_usd = tonumber(ARGV[3])
    local tpm_limit = tonumber(ARGV[4])
    local monthly_token_limit = tonumber(ARGV[5])
    local monthly_usd_limit = tonumber(ARGV[6])
    local reservation_id = ARGV[7]
    local now_minute = ARGV[8]
    local now_month = ARGV[9]

    local tpm_key = "waddleai:tpm:" .. vkey_id .. ":" .. now_minute
    local tok_key = "waddleai:budget:tok:" .. vkey_id .. ":" .. now_month
    local usd_key = "waddleai:budget:usd:" .. vkey_id .. ":" .. now_month

    -- Phase 1: read-only checks. Nothing is mutated until EVERY limit has passed (Bug A).
    if tpm_limit > 0 then
        local current = tonumber(redis.call("GET", tpm_key)) or 0
        if current + estimated_tokens > tpm_limit then
            return {0, "tpm_exceeded", ""}
        end
    end
    if monthly_token_limit > 0 then
        local current = tonumber(redis.call("GET", tok_key)) or 0
        if current + estimated_tokens > monthly_token_limit then
            return {0, "monthly_tokens_exceeded", ""}
        end
    end
    if monthly_usd_limit > 0 then
        local current = tonumber(redis.call("GET", usd_key)) or 0
        if current + estimated_usd > monthly_usd_limit then
            return {0, "monthly_usd_exceeded", ""}
        end
    end

    -- Phase 2: every check passed; apply all mutations.
    if tpm_limit > 0 then
        redis.call("INCRBY", tpm_key, estimated_tokens)
        redis.call("EXPIRE", tpm_key, 60)
    end
    if monthly_token_limit > 0 then
        redis.call("INCRBY", tok_key, estimated_tokens)
        redis.call("EXPIRE", tok_key, 2592000)  -- 30 days
    end
    if monthly_usd_limit > 0 then
        redis.call("INCRBYFLOAT", usd_key, estimated_usd)
        redis.call("EXPIRE", usd_key, 2592000)  -- 30 days
    end

    local resv_key = "waddleai:resv:" .. reservation_id
    redis.call("SET", resv_key, cjson.encode({
        vkey_id = vkey_id,
        estimated_tokens = estimated_tokens,
        estimated_usd = estimated_usd,
        created_at = redis.call("TIME")[1]
    }), "EX", 3600)  -- 1 hour TTL

    return {1, "", reservation_id}
    """
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/integration/quota/test_limiter_regressions.py tests/unit/test_token_limiter.py -v --no-cov`
Expected: PASS (all). If a unit test asserted the old `cjson.null` third element, update that assertion to the new `""` shape (the unit tests mock `evalsha`, so only hand-built return tuples matter).

- [ ] **Step 5: Commit**

```bash
git add shared/utils/token_limiter.py tests/integration/quota/test_limiter_regressions.py tests/unit/test_token_limiter.py
git commit -m "fix(limiter): check all limits before any INCRBY (Bug A) + real-Valkey regression"
git push
```

---

### Task 3: TokenLimiter Bug B — reconcile against the reservation's stored window

Spec §6 Bug B: `reconcile` computes `now_minute`/`now_month` from the reconcile-time clock, so a reservation made at 23:59 on the last day of a month adjusts the wrong month's counters (and can create negative stray counters).

**Files:**
- Modify: `shared/utils/token_limiter.py` (`LUA_RESERVE` resv SET block, `LUA_RECONCILE`, `reserve()` signature, `reconcile()` call)
- Modify: `tests/integration/quota/test_limiter_regressions.py` (append)
- Modify: `tests/unit/test_token_limiter.py` (reconcile argument-count assertions, if any)

**Interfaces:**
- Consumes: Task 2's `LUA_RESERVE`.
- Produces: reservation JSON now also stores `now_minute` and `now_month`; `LUA_RECONCILE` takes only `ARGV = [reservation_id, actual_tokens, actual_usd]` and adjusts the stored window; `TokenLimiter.reserve(self, vkey_id, estimated_tokens, estimated_usd, limits, *, now: datetime | None = None) -> GateDecision` (the `now` seam makes the window testable).

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/quota/test_limiter_regressions.py`:

```python
from datetime import datetime


async def test_regression_bug_b_reconcile_uses_reservation_window_not_reconcile_clock(valkey_async):
    """Reserved on 2026-01-31 23:59; reconciled 'now' (a later month): January counters adjust."""
    limiter = TokenLimiter(valkey_async, _FlagOn())
    limits = KeyLimits(tpm_limit=1000, monthly_token_limit=10_000, monthly_usd_limit=None)
    decision = await limiter.reserve(
        11, 100, 0.0, limits, now=datetime(2026, 1, 31, 23, 59, 30)
    )
    assert decision.allowed is True
    await limiter.reconcile(decision.reservation_id, 40, 0.0)
    assert int(await valkey_async.get("waddleai:budget:tok:11:202601")) == 40
    assert int(await valkey_async.get("waddleai:tpm:11:202601312359")) == 40
    # No stray counter (e.g. a negative delta) was created in the reconcile-time window.
    assert await _keys(valkey_async, "waddleai:*:11:*") == [
        "waddleai:budget:tok:11:202601",
        "waddleai:tpm:11:202601312359",
    ]
    assert await _keys(valkey_async, "waddleai:resv:*") == []


async def test_reconcile_does_not_resurrect_an_expired_tpm_window(valkey_async):
    """If the 60s TPM key already expired, reconcile must not recreate it (no TTL, never expires)."""
    limiter = TokenLimiter(valkey_async, _FlagOn())
    limits = KeyLimits(tpm_limit=1000, monthly_token_limit=None, monthly_usd_limit=None)
    decision = await limiter.reserve(12, 100, 0.0, limits, now=datetime(2026, 1, 5, 10, 0, 0))
    await valkey_async.delete("waddleai:tpm:12:202601051000")  # simulate the 60s TTL elapsing
    await limiter.reconcile(decision.reservation_id, 10, 0.0)
    assert await _keys(valkey_async, "waddleai:tpm:12:*") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/integration/quota/test_limiter_regressions.py -k "bug_b or resurrect" -v --no-cov`
Expected: FAIL — `TypeError: reserve() got an unexpected keyword argument 'now'`.

- [ ] **Step 3: Write minimal implementation**

In `LUA_RESERVE`, replace the `redis.call("SET", resv_key, cjson.encode({...}))` block with:

```lua
    local resv_key = "waddleai:resv:" .. reservation_id
    redis.call("SET", resv_key, cjson.encode({
        vkey_id = vkey_id,
        estimated_tokens = estimated_tokens,
        estimated_usd = estimated_usd,
        now_minute = now_minute,
        now_month = now_month,
        created_at = redis.call("TIME")[1]
    }), "EX", 3600)  -- 1 hour TTL
```

Replace the entire `LUA_RECONCILE` string with:

```python
    LUA_RECONCILE = """
    local reservation_id = ARGV[1]
    local actual_tokens = tonumber(ARGV[2])
    local actual_usd = tonumber(ARGV[3])

    local resv_key = "waddleai:resv:" .. reservation_id
    local resv_data = redis.call("GET", resv_key)
    if not resv_data then
        return {0, "reservation_not_found"}
    end

    local resv = cjson.decode(resv_data)
    local token_delta = actual_tokens - resv.estimated_tokens
    local usd_delta = actual_usd - resv.estimated_usd

    -- Adjust the window the reservation was MADE in (Bug B), never the reconcile-time clock.
    local tpm_key = "waddleai:tpm:" .. resv.vkey_id .. ":" .. resv.now_minute
    local tok_key = "waddleai:budget:tok:" .. resv.vkey_id .. ":" .. resv.now_month
    local usd_key = "waddleai:budget:usd:" .. resv.vkey_id .. ":" .. resv.now_month

    -- The TPM key has a 60s TTL: only adjust it while it still exists (INCRBY would recreate
    -- it without a TTL). The budget keys are only ever created by a reserve with that limit.
    if token_delta ~= 0 and redis.call("EXISTS", tpm_key) == 1 then
        redis.call("INCRBY", tpm_key, token_delta)
    end
    if token_delta ~= 0 and redis.call("EXISTS", tok_key) == 1 then
        redis.call("INCRBY", tok_key, token_delta)
    end
    if usd_delta ~= 0 and redis.call("EXISTS", usd_key) == 1 then
        redis.call("INCRBYFLOAT", usd_key, usd_delta)
    end

    redis.call("DEL", resv_key)
    return {1, ""}
    """
```

Change `reserve()`'s signature and clock lines:

```python
    async def reserve(
        self,
        vkey_id: int,
        estimated_tokens: int,
        estimated_usd: float,
        limits: KeyLimits,
        *,
        now: datetime | None = None,
    ) -> GateDecision:
```

and replace `now = datetime.utcnow()` inside `reserve()` with `now = now or datetime.utcnow()` (rename the local to `moment` to avoid shadowing: `moment = now or datetime.utcnow()`, then `now_minute = moment.strftime("%Y%m%d%H%M")`, `now_month = moment.strftime("%Y%m")`).

In `reconcile()`, delete the `now = datetime.utcnow()` / `now_minute` / `now_month` lines and the two trailing args so the call is:

```python
            await self.valkey.evalsha(
                self._reconcile_sha,
                0,  # numkeys=0
                reservation_id,
                str(actual_tokens),
                str(actual_usd),
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/integration/quota/test_limiter_regressions.py tests/unit/test_token_limiter.py -v --no-cov`
Expected: PASS. Fix any `tests/unit/test_token_limiter.py` assertion that counts reconcile `evalsha` positional args (5 -> 3) in the same edit.

- [ ] **Step 5: Commit**

```bash
git add shared/utils/token_limiter.py tests/integration/quota/test_limiter_regressions.py tests/unit/test_token_limiter.py
git commit -m "fix(limiter): reconcile against the reservation's stored window (Bug B)"
git push
```
