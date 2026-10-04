# Feature Flag and License Check Resilience

How `shared.utils.feature_flags.is_feature_enabled()` and
`shared.licensing.python_client.PenguinTechLicenseClient.check_feature()`
degrade when PostHog or the license server are unreachable, and the env
vars that tune that behaviour. Both mechanisms share one principle: **an
outage degrades to the last-known-good answer, never straight to a
hardcoded default** — a flag-store or license-server outage must not
silently disable a feature, or lock a paying tenant out of one, that was
working a moment ago.

## Feature flags (`shared/utils/feature_flags.py`)

Evaluation order, every call:

1. **Env override** — `WADDLEAI_FLAG_<NAME>` (e.g. `waddleai.memory-org-scope`
   -> `WADDLEAI_FLAG_MEMORY_ORG_SCOPE`). Always wins, bypasses the cache and
   PostHog entirely. Used by tests and alpha environments.
2. **TTL-fresh cache hit** — no PostHog round trip while a resolved value is
   still within `FEATURE_FLAG_TTL_SECONDS`.
3. **Live PostHog lookup** — bounded by `FEATURE_FLAG_TIMEOUT_SECONDS`.
4. **Outage fallback** — a PostHog exception serves the last-known cached
   value for that (flag, distinct_id), regardless of TTL freshness.
5. **Caller default** — only when nothing was ever successfully resolved
   (never-seen flags default OFF, per house rules).

| Env var | Default | Purpose |
|---|---|---|
| `FEATURE_FLAG_TTL_SECONDS` | `30` | How long a resolved value is served without re-checking PostHog |
| `FEATURE_FLAG_TIMEOUT_SECONDS` | `3` | Per-request timeout passed to PostHog's feature-flag HTTP call |
| `FEATURE_FLAG_WARN_INTERVAL_SECONDS` | `60` | Minimum gap between outage WARNINGs for the same flag (rate-limited, not once-per-call) |
| `FEATURE_FLAG_CACHE_MAX_ENTRIES` | `2048` | Bound on the resolved-value cache (LRU eviction) across all (flag, distinct_id) pairs |
| `WADDLEAI_FLAG_DISABLE_FLAG_DEGRADATION_CACHE` | unset (OFF) | Kill switch: `1`/`true` reverts to the pre-fix behaviour (no cache, no last-known fallback, straight to the caller's default on any PostHog error) |

Metric: `feature_flag_evaluations_total{result}`, `result` ∈
`live` (fresh env/PostHog answer), `cached` (TTL-fresh hit or outage
last-known), `default` (deliberately unconfigured/undefined flag), `error`
(outage with no last-known value). Never labelled by flag key or value.

## License entitlement checks (`shared/licensing/python_client.py`)

`PenguinTechLicenseClient.check_feature()` caches entitlements for 5 minutes
(`_cache_ttl`). On a license-server outage (`requests.RequestException`)
after that TTL expires:

- A previously-fetched entitlement younger than `LICENSE_MAX_STALE_SECONDS`
  is served as-is (stale-while-error), logged at WARNING.
- An entitlement older than that window, or a feature that was **never**
  successfully fetched, hard-denies (logged at ERROR).

| Env var | Default | Purpose |
|---|---|---|
| `LICENSE_MAX_STALE_SECONDS` | `604800` (7 days) | Oldest a cached entitlement may be and still be served during an outage |
| `WADDLEAI_FLAG_DISABLE_LICENSE_STALE_CACHE` | unset (OFF) | Kill switch: `1`/`true` reverts to hard-denying immediately on any request exception, no stale serving |

Metric: `license_checks_total{result}`, `result` ∈ `live` (fresh fetch or
TTL-fresh cache hit), `stale` (served past TTL during an outage), `denied`
(hard denial), `bypass` (reserved for the domain-based bypass recorded by
callers using `penguin_licensing.LicenseClient` / `shared/licensing/
domain_bypass.py` — a different client this module does not implement).

The downstream wrappers that already layer their own last-known-value
fallback on top of `check_feature()` —
`ContentFilter._ner_tier_enabled`/`UsageTracker._has_premium`
(`shared/licensing/gate_cache.py`) — need no changes: they previously never
saw a real outage signal (a swallowed `RequestException` returned a
confident `False`, poisoning their own cache); this fix makes
`check_feature()`'s return value accurate during an outage, which those
callers' existing cache-and-fallback logic already handles correctly.

## Related

- [`CONFIGURATION.md`](./CONFIGURATION.md) — platform-wide env var/flag index for the
  2026-10 ops-remediation pass (proxy auth cache, Hypercorn workers, dangling
  alert references).
- `services/management/app/extensions.py` — DB init retry (`DB_MAX_RETRIES`,
  `DB_RETRY_DELAY`, `DB_RETRY_MAX_DELAY`, exponential backoff + full jitter)
  and the Valkey/Redis connection pool bound
  (`MANAGEMENT_VALKEY_MAX_CONNECTIONS`,
  `MANAGEMENT_VALKEY_SOCKET_TIMEOUT`, `MANAGEMENT_VALKEY_CONNECT_TIMEOUT`,
  `MANAGEMENT_VALKEY_HEALTH_CHECK_INTERVAL`).
- `services/management/app/config.py` — `MANAGEMENT_MAX_BODY_BYTES`
  (default 2 MiB) caps request body size; an oversized request 413s in the
  standard `{"error", "message"}` shape.
