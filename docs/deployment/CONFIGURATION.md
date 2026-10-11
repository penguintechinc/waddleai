# Platform Configuration Reference (Proxy & Management)

Authoritative index of environment variables and PostHog feature flags added or
changed by the 2026-10 operational-readiness remediation pass (`gh-261`–`gh-276`).
Every default below is verified against the code cited next to it — this page
doesn't duplicate the per-feature docs it links to; it's the map back to them.

For the penguincode CLI/server's own configuration, see
[`docs/penguincode/CONFIGURATION.md`](../penguincode/CONFIGURATION.md) instead — that
doc is already the authoritative reference for `PENGUINCODE_*` env vars and
`penguincode.*` flags.

## Index of existing per-feature docs

| Topic | Doc |
|---|---|
| Proxy Prometheus metrics, concurrency/body/Valkey tunables, Ollama-embedding bulkhead | [`proxy-observability.md`](./proxy-observability.md) |
| Feature-flag and license-check outage degradation (`FEATURE_FLAG_*`, `LICENSE_MAX_STALE_SECONDS`, management DB/Valkey retry) | [`feature-flags-and-licensing-resilience.md`](./feature-flags-and-licensing-resilience.md) |
| Helm chart values (probes, PDBs, HPA, `monitoring.*`, `ollamaEmbeddings.*`) | [`docs/docs-site/docs/deployment/kubernetes.md`](../docs-site/docs/deployment/kubernetes.md) |
| Alerting thresholds and runbook | [`docs/operations/MONITORING.md`](../operations/MONITORING.md), [`SLOS.md`](../operations/SLOS.md) |
| OpenAI/Anthropic-compatible API (SSE streaming, `Retry-After`, 429 shape) | [`docs/api/openai-compatible.md`](../api/openai-compatible.md) |

## Ollama endpoints (config-hygiene ops-audit 2026-10-09)

"Where is Ollama" was spelled five different ways across the repo
(`OLLAMA_URL`, `OLLAMA_API_URL`, `OLLAMA_HOST`, `OLLAMA_BASE_URL`,
`OLLAMA_EMBEDDING_URL`, `PENGUINCODE_EMBEDDING_OLLAMA_URL`) — several
defaulting to `localhost`, so a process that set one name could still
silently land on localhost via a different code path reading another name.
Every reader now resolves through one shared helper
(`shared/utils/ollama_endpoint.py`; PenguinCode carries a tested twin at
`services/penguincode/penguincode_cli/config/ollama_endpoint.py`, since it
cannot import this repo's root `shared/`), which logs the resolved URL and
the env var that supplied it once at INFO, plus a one-time DEPRECATION
warning when a legacy name wins.

| Env var | Scope | Precedence | Default |
|---|---|---|---|
| `WADDLEAI_OLLAMA_URL` | Chat/completions — **canonical** | 1st | — |
| `OLLAMA_API_URL` | Chat/completions — legacy | 2nd | — |
| `OLLAMA_URL` | Chat/completions — legacy | 3rd | — |
| `OLLAMA_HOST` | Chat/completions — legacy | 4th | — |
| `OLLAMA_BASE_URL` | Chat/completions — legacy (the proxy's own prior, undocumented spelling for its security-auditor Ollama call) | 5th | — |
| (none set) | Chat/completions | — | `http://localhost:11434` |
| `WADDLEAI_OLLAMA_EMBEDDING_URL` | Embedding bulkhead (ops-audit O10/O5) — **canonical** | 1st | — |
| `OLLAMA_EMBEDDING_URL` | Embedding bulkhead — legacy | 2nd | — |
| `PENGUINCODE_EMBEDDING_OLLAMA_URL` | Embedding bulkhead — legacy | 3rd | — |
| (none set) | Embedding bulkhead | — | the resolved chat URL above |

No breaking change: every legacy name keeps working at lower precedence than
the canonical name. New deployments should set `WADDLEAI_OLLAMA_URL` (and
`WADDLEAI_OLLAMA_EMBEDDING_URL` when enabling the embedding bulkhead, see
`ollamaEmbeddings` in `k8s/helm/waddleai/values.yaml`); existing deployments
setting a legacy name see a DEPRECATION warning but no functional change.
Readers wired to this chain: PenguinCode's `OllamaConfig`
(`penguincode_cli/config/settings.py`), the `ollama_ready`
test fixture and its two dependent live-embedding test files
(`services/penguincode/tests/{conftest,test_memory,test_retrieval_graphrag}.py`),
`shared/utils/embedding_manager.py`, and the proxy's Ollama-backed security
auditor (`proxy/apps/proxy_server/main.py`). See
[`docs/penguincode/CONFIGURATION.md`](../penguincode/CONFIGURATION.md#ollama-endpoints)
for the PenguinCode-side mirror of this table.

## Proxy: API-key auth cache (ops O7-a/O11)

Not yet documented elsewhere — added here. See
`proxy/apps/proxy_server/auth_cache.py` module docstring for the full design
(off-event-loop executor + Valkey-backed cache fronting the DB+bcrypt lookup on
every authenticated proxy request).

| Env var | Default | Purpose |
|---|---|---|
| `PROXY_AUTH_EXECUTOR_WORKERS` | `8` | Dedicated `ThreadPoolExecutor` size for the DB lookup, bcrypt verify, and debounced `last_used` write — never the default `asyncio.to_thread` pool. |
| `PROXY_AUTH_CACHE_TTL_SECONDS` | `60` | Cache-hit lifetime for a verified key's record. Also the documented worst-case staleness bound for a revoked key when Valkey invalidation itself can't be reached. |
| `PROXY_AUTH_NEGATIVE_CACHE_TTL_SECONDS` | `5` | Cache lifetime for a failed lookup (unknown/invalid `key_id`) — short, to avoid masking a just-created key for long. |
| `PROXY_AUTH_LAST_USED_INTERVAL_SECONDS` | `60` | Minimum gap between `last_used` DB writes for the same key, debounced in the background, never inline on the request path. |

| Flag | Default | Effect |
|---|---|---|
| `waddleai.disable-auth-cache` | unseen/OFF = cache mechanism ON | Opt-out kill-switch. ON bypasses the cache entirely, falling back to a per-request DB+bcrypt lookup (still executor-offloaded — the event-loop fix itself is not optional). |

Revocation: `DELETE /api/v1/proxy-keys/{key_id}` (`services/management/app/api/v1/proxy_keys.py`) best-effort-invalidates the Valkey entry immediately; `PROXY_AUTH_CACHE_TTL_SECONDS` is the fallback bound when that invalidation can't reach Valkey. New metric: `waddleai_auth_lookup_duration_seconds{result}` (`result` ∈ `hit`/`miss`/`negative`/`bypass`) — not yet listed in `proxy-observability.md`'s metrics table; cross-reference there.

## Hypercorn worker count (ops O9)

Previously hardcoded `--workers` baked into each Dockerfile's `CMD`; now runtime-overridable.

| Env var | Service | Default | Helm value | Purpose |
|---|---|---|---|---|
| `HYPERCORN_WORKERS` | proxy | `4` | `proxy.workers` | Hypercorn worker-process count (`proxy/Dockerfile`). |
| `HYPERCORN_WORKERS` | management | `2` | `management.workers` | Same, `services/management/Dockerfile`. |
| `HYPERCORN_GRACEFUL_TIMEOUT` | proxy only | `30` | `proxy.gracefulTimeoutSeconds` | Hypercorn's own `--graceful-timeout` drain window; management's Dockerfile doesn't pass `--graceful-timeout` at all. |

Both are per-Hypercorn-**worker-process** bounds, not cluster-wide — see
`proxy-observability.md`'s `PROXY_MAX_CONCURRENT_PER_WORKER` note for the same
caveat applied to the concurrency limiter. Full probe/PDB/grace-period context:
`kubernetes.md` Configuration table.

## Known dangling alert/metric references

See [`docs/operations/MONITORING.md`](../operations/MONITORING.md#known-gap-dangling-alertpanel-metric-references)
— two PrometheusRule alerts (`ProxyInflightSaturationHigh`'s denominator,
`Management/ProxyDBPoolSaturated`) and two penguincode-specific alerts
(`PenguinCodeFeatureFlagEvalErrors`/`LicenseCheckErrors`) reference metrics that
don't exist for the service in question. Each is marked inline in its Helm
template; this is a known follow-up, not something this sweep could close
without adding new instrumentation to application code.
