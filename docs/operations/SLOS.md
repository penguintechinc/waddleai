# Service Level Objectives

SLI/SLO definitions for the three production services (management, proxy, penguincode)
backing the alerts shipped in `k8s/helm/waddleai/templates/monitoring/prometheusrule-*.yaml`
and `services/penguincode/k8s/helm/penguincode/templates/monitoring/prometheusrule-server.yaml`
(O2 fix — see `docs/operations/MONITORING.md` for the alert-by-alert runbook). All targets
and thresholds below are values-driven (`monitoring.prometheusRule.*.slo` in each chart's
`values.yaml`) — the numbers here are the shipped defaults, not hardcoded.

## Error-budget burn derivation

Every error-rate alert uses the Google SRE workbook's **multi-window multi-burn-rate**
method against a 30-day rolling error budget. A plain "error ratio > X for 5 minutes"
alert either pages on a blip (too sensitive) or misses a slow bleed (too insensitive);
requiring BOTH a short window and a long window to exceed the same threshold catches
both without either failure mode.

For an availability target `T` (e.g. 99.9%), the error budget is `E = 1 - T` (0.001).
For a long window of length `W` (as a fraction of the 30-day budget period) and a
target fraction of the 30-day budget `B` to have been consumed if the alert is real,
the burn-rate threshold is:

```
threshold = B * (30d / W) * E
```

This repo's windows are **2h / 6h / 1d / 3d** (not the classic Google 1h/6h/1d/3d table,
adapted to the same shape) with short windows at `long/12`:

| Tier | Long window | Short window | Budget consumed if sustained | Threshold (99.9% target) | Severity |
|------|------------|--------------|-------------------------------|---------------------------|----------|
| Fast | 2h | 10m | 2% in 2h | 0.0072 (0.72%) | critical |
| Mid | 6h | 30m | 5% in 6h | 0.006 (0.6%) | critical |
| Slow | 1d | 2h | 10% in 1d | 0.003 (0.3%) | warning |
| Slowest | 3d | 6h | 10% in 3d | 0.001 (0.1%) | warning |

An alert fires only when **both** the long-window AND short-window error ratio exceed the
threshold — the short window confirms the condition is still true right now (not a burst
that already ended), the long window confirms it isn't a 30-second blip.

## Management

| SLI | PromQL | Target | Error budget | Alerting policy |
|-----|--------|--------|---------------|------------------|
| Availability | `up{job="waddleai-management"}` | 99.9% | 0.1% / 30d (~43min/month) | `ManagementTargetDown` (5m), `ManagementMetricsAbsent` (10m) |
| Error rate | `sum(rate(waddleai_requests_total{service="management",status_code=~"5.."}[w])) / sum(rate(waddleai_requests_total{service="management"}[w]))` | 99.9% success | 0.1% / 30d | `ManagementErrorBudgetBurn{Fast,Mid,Slow,Slowest}` |
| Latency p95 | `histogram_quantile(0.95, sum(rate(waddleai_request_duration_seconds_bucket{service="management"}[15m])) by (le))` | ≤ 2s | n/a (threshold SLO, not burn-rate) | `ManagementLatencyP95High` (15m sustained) |
| Latency p99 | `histogram_quantile(0.99, ...)` | ≤ 5s | n/a | `ManagementLatencyP99High` (15m sustained) |

Cause alerts (warning, explain a symptom, never page alone): `ManagementDatabaseDown`,
`ManagementRedisDown`, `ManagementDBPoolSaturated`, `ManagementPodRestartingFrequently`,
`ManagementHPAAtMax`, `ManagementPDBViolated`, `ManagementFeatureFlagEvalErrors`,
`ManagementLicenseCheckErrors`.

## Proxy

Proxy splits its latency SLO in two: **non-LLM routes** (health, models, usage, quota,
routing stats — proxy-controlled latency) get the house default; **LLM routes**
(`/v1/chat/completions`, `/v1/messages`, `/v1/messages/count_tokens`) get their own,
looser budget because upstream provider latency dominates and is outside this service's
control — folding them into one SLO would either make the non-LLM budget meaningless or
page on every slow upstream provider.

| SLI | PromQL | Target | Error budget | Alerting policy |
|-----|--------|--------|---------------|------------------|
| Availability | `up{job="waddleai-proxy"}` | 99.9% | 0.1% / 30d | `ProxyTargetDown` (5m), `ProxyMetricsAbsent` (10m) |
| Error rate | same shape as management, `service="proxy"` | 99.9% success | 0.1% / 30d | `ProxyErrorBudgetBurn{Fast,Mid,Slow,Slowest}` |
| Latency p95 (non-LLM) | `histogram_quantile(0.95, ...{endpoint!~"/v1/chat/completions\|/v1/messages\|/v1/messages/count_tokens"}...)` | ≤ 2s | n/a | `ProxyLatencyP95High` |
| Latency p99 (non-LLM) | same, 0.99 | ≤ 5s | n/a | `ProxyLatencyP99High` |
| Latency p95 (LLM routes) | same shape, `endpoint=~"..."` (LLM paths only) | ≤ 30s | n/a | `ProxyLLMLatencyP95High` |
| Saturation (in-flight) | `waddleai_proxy_inflight_requests / proxy_concurrency_limit` | ≤ 90% | n/a | `ProxyInflightSaturationHigh` *(known dangling reference -- `proxy_concurrency_limit` doesn't exist; see MONITORING.md "Known gap")* |

Cause alerts: `ProxyProviderUnhealthy`, `ProxyDBPoolSaturated` *(known dangling
reference)*, `ProxyRateLimitExceededHigh`, `ProxyCacheHitRateLow`,
`ProxyConcurrencyRejectionsHigh`, `ProxyPodRestartingFrequently`, `ProxyHPAAtMax`,
`ProxyPDBViolated`, `ProxyFeatureFlagEvalErrors`, `ProxyLicenseCheckErrors`.

## PenguinCode server

| SLI | PromQL | Target | Error budget | Alerting policy |
|-----|--------|--------|---------------|------------------|
| Availability | `up{job="penguincode-server"}` | 99.9% | 0.1% / 30d | `PenguinCodeTargetDown` (5m), `PenguinCodeMetricsAbsent` (10m) |
| Error rate (gRPC) | `sum(rate(rpc_server_requests_total{job="penguincode-server",status!="OK"}[w])) / sum(rate(rpc_server_requests_total{job="penguincode-server"}[w]))` | 99.9% success | 0.1% / 30d | `PenguinCodeErrorBudgetBurn{Fast,Slow,Slowest}` |
| Latency p95 | `histogram_quantile(0.95, sum(rate(rpc_server_duration_seconds_bucket{job="penguincode-server"}[15m])) by (le))` | ≤ 5s | n/a | `PenguinCodeLatencyP95High` |
| Latency p99 | same, 0.99 | ≤ 15s | n/a | `PenguinCodeLatencyP99High` |
| Saturation (index queue) | `index_queue_depth{job="penguincode-server"}` | ≤ 50 pending jobs | n/a | `PenguinCodeIndexQueueDepthHigh` (10m sustained) |

Cause alerts: `PenguinCodeIndexJobFailuresHigh`, `PenguinCodeDBPoolSaturated`,
`PenguinCodePodRestartingFrequently`, `PenguinCodeHPAAtMax`, `PenguinCodePDBViolated`,
`PenguinCodeFeatureFlagEvalErrors` *(known dangling reference)*,
`PenguinCodeLicenseCheckErrors` *(known dangling reference)*.

PenguinCode's gRPC/queue/pool metrics and its `/metrics` Prometheus scrape route both
exist today (`penguincode_cli/server/rest_app.py`, `penguincode_cli/observability/
otel.py`) — see `docs/operations/MONITORING.md` "Known gap" section for the two
metrics (`feature_flag_evaluations_total`/`license_checks_total`) that remain
unimplemented for penguincode specifically.

## HPA-at-max / PDB-violated coverage

Management, proxy, and the penguincode server all have an HPA
(`{management,proxy}-hpa.yaml`, penguincode's `templates/hpa.yaml`) and a
PodDisruptionBudget (`{management,proxy}-pdb.yaml`, penguincode's
`templates/pdb.yaml`), each paired with a `*HPAAtMax`/`*PDBViolated` alert in its
PrometheusRule.
