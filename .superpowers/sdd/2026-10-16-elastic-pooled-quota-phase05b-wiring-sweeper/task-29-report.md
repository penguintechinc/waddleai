# Task 29 report: expiry sweeper

Status: DONE_WITH_CONCERNS

TDD: RED = ModuleNotFoundError on quota_sweeper at collection. GREEN = 11 passed (9 unit + 2 real-Valkey integration).

Files: proxy/apps/proxy_server/quota_sweeper.py (new); main.py (+13: import, 2 attrs, startup task gated on quota_limiter is not None, shutdown first statement stop); tests/unit/proxy/test_quota_sweeper.py; tests/integration/quota/test_quota_sweeper_real.py (real Valkey: expired reservation refunded, live untouched).

Confirmations
- Cadence: default 60s (env PROXY_QUOTA_SWEEP_INTERVAL_SECONDS; invalid/<1 -> 60), well under the 600s grace; pinned by test.
- Refund-only: loop only calls limiter.sweep_expired (Task 22 refund Lua), batch=100 per tick (bounded).
- Fail-safe: exception per pass logged at ERROR, loop continues (tested). sweep_expired also swallows index-read errors itself.
- Clean stop: stop event + cancel + await, CancelledError suppressed; None no-op; wait uses wait_for(stop.wait()) so stop is prompt.
- No-op without engine: task started only when self.quota_limiter is not None.

Concerns
- tests/unit/proxy/test_proxy_server_main.py cannot collect here (penguin_aaa not installed in env, pre-existing), so main.py wiring was only syntax-checked + ruff, not run through that suite.
- Sweep batch is 100/tick; at 60s that is ~100 refunds/min/replica, a very large abandoned backlog drains slowly (not tuned; brief fixed batch).
- mypy clean on quota_sweeper.py; main.py not mypy-checked (deps missing).

## Fix round 1
- Fix 1: interval falls back to 60 when unparseable, non-finite, <1 or >=600 (never >= leak window); tests for 900/600/inf/nan/abc/0.2/-3 -> 60, 120/599/5 kept.
- Fix 2: stop_quota_sweeper now cancels then `await asyncio.wait({task})` (never re-raises the task's own CancelledError, so a caller cancel propagates); test with a stubborn task proves caller cancellation is not swallowed. Sweeper start moved to the very end of startup() so a failed startup cannot orphan it.
- Fix 3: sweep-failure ERROR log uses exc_info=True; test asserts exc_info present.
- Result: 19 passed (unit + real Valkey), ruff + mypy clean on touched files. main.py startup ordering only syntax-checked (penguin_aaa missing in env).
