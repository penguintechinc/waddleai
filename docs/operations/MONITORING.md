# Monitoring

Observability baseline (O2 fix) for the three production services: management, proxy,
and the penguincode server. Covers how to enable it, what dashboards exist, and a
first-response runbook entry for every alert. SLI/SLO definitions and error-budget-burn
derivation live in [`docs/operations/SLOS.md`](./SLOS.md).

## Enabling

Everything below is OFF by default in every chart (`monitoring.enabled: false` in
`k8s/helm/waddleai/values.yaml` and `services/penguincode/k8s/helm/penguincode/values.yaml`)
so `helm template`/`helm install` renders cleanly on a cluster that has never installed
the [Prometheus Operator](https://github.com/prometheus-operator/prometheus-operator)
CRDs (local/alpha). `values-beta.yaml` (waddleai) and `beta.yml`/`gamma.yml`/`production.yml`
(penguincode) flip `monitoring.enabled: true` because dal2-beta (and DO gamma/prod) run
[kube-prometheus-stack](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack),
which bundles the operator, a Grafana sidecar watching `grafana_dashboard: "1"` ConfigMaps,
and kube-state-metrics (needed for the restart/HPA/PDB cause alerts below).

To enable on a cluster not already covered by a values file:

```bash
helm upgrade --install waddleai ./k8s/helm/waddleai \
  --values ./k8s/helm/waddleai/values-beta.yaml \
  --set monitoring.enabled=true \
  --set monitoring.extraLabels.release=<your-prometheus-operator-release-name>
```

Sub-toggles, independent of the master switch: `monitoring.serviceMonitor.enabled`,
`monitoring.prometheusRule.enabled`, `monitoring.dashboards.enabled` — e.g. keep
dashboards but skip alerting while iterating on thresholds.

**Prerequisite:** the Prometheus Operator's Prometheus CR must actually watch this
release's namespace/labels (`serviceMonitorNamespaceSelector`, `ruleNamespaceSelector`,
and a `serviceMonitorSelector`/`ruleSelector` matching `monitoring.extraLabels`) — a
`ServiceMonitor` the operator never selects is a silent no-op, not an error.

## Dashboards

Grafana auto-discovers these via the sidecar label; no manual import needed once the
sidecar is running and watching this namespace.

| Dashboard | Chart | Covers |
|-----------|-------|--------|
| WaddleAI / Management | waddleai | RED (rate/errors/duration), latency heatmap, DB/Redis dependency health, DB operation latency, uptime, flag/license error rates |
| WaddleAI / Proxy | waddleai | RED, non-LLM + LLM latency percentiles, latency heatmap, in-flight concurrency vs. cap, provider health, response-cache hit rate, DB pool waiters, flag/license error rates |
| WaddleAI / Platform Overview | waddleai | Request rate / error ratio / p95 latency by service, scrape-target `up`, pod restarts by workload, a table of currently-firing alerts |
| PenguinCode / Server | penguincode | gRPC RED, latency percentiles + heatmap, index queue depth/jobs, DB pool waiters, flag/license error rates |

## Known gap: dangling alert/panel metric references

Two kinds of reference in the `ServiceMonitor`/`PrometheusRule`/dashboard manifests
point at metrics that don't exist for every service that uses them. Each is marked
inline in its Helm template with a `# Known dangling reference` (or equivalent) comment
— PromQL against an absent series returns no data, not an error, so these alerts
silently never fire rather than erroring loudly.

| Metric | Exists for | Missing for | Affected alerts/panels |
|---|---|---|---|
| `db_pool_waiting` | penguincode (`penguincode_cli/observability/otel.py`) | management, proxy — no equivalent gauge was ever added to `shared/utils/metrics.py` | `ManagementDBPoolSaturated`, `ProxyDBPoolSaturated` (never fire); `PenguinCodeDBPoolSaturated` works |
| `proxy_concurrency_limit` | nowhere | everywhere — no gauge for the configured `PROXY_MAX_CONCURRENT_PER_WORKER` cap exists | `ProxyInflightSaturationHigh` (never fires; the numerator, `waddleai_proxy_inflight_requests`, does exist) |
| `feature_flag_evaluations_total` / `license_checks_total` | management, proxy (`shared/utils/feature_flags.py`, `shared/licensing/python_client.py`) | penguincode — `penguincode_cli/flags/client.py` never records them | `PenguinCodeFeatureFlagEvalErrors`, `PenguinCodeLicenseCheckErrors` (never fire); the management/proxy equivalents work |

`rpc_server_duration_seconds`, `rpc_server_requests_total`, `index_queue_depth`,
`index_jobs_total`, and penguincode's own `db_pool_waiting` are all real today, served
via `penguincode_cli/server/rest_app.py`'s `GET /metrics` route (Prometheus text format,
opt-out kill-switch `penguincode.disable-prometheus-metrics`) alongside OTLP push —
`PenguinCodeTargetDown`/`PenguinCodeMetricsAbsent` reflect genuine scrape health now,
not an expected-on-every-install false positive.

## Alert runbook

Every alert below carries a `service` and `slo` label (`error-budget-burn`, `latency`,
`latency-llm`, `saturation`, `availability`, `cause`) and a `severity`
(`critical`/`warning`). Cause alerts explain a symptom alert; they're also useful as an
early-warning signal on their own before a symptom alert fires.

### Management

**ManagementErrorBudgetBurnFast / Mid / Slow / Slowest** — The management service's 5xx
ratio has exceeded its error-budget-burn-rate threshold over both a short and long window
(see SLOS.md for exact windows/thresholds per tier; Fast/Mid page, Slow/Slowest ticket).
Means some fraction of management API calls are failing with a server error, consistently,
not as a blip. First checks: (1) `kubectl logs -n waddleai deploy/waddleai-management
--tail=200` for the actual exception; (2) the Platform Overview dashboard's per-service
error ratio panel to confirm it's management and not a shared dependency (Postgres/Redis)
taking every service down together; (3) whether `ManagementDatabaseDown`/
`ManagementRedisDown`/`ManagementDBPoolSaturated` are also firing — if so, the dependency
is the root cause, not management code. Mitigation: roll back the last management
deployment if it correlates with a release; scale out if it's load-related and the HPA
hasn't already maxed (`ManagementHPAAtMax`); if a specific new code path is implicated and
it sits behind a feature flag, flip that flag off rather than a full rollback.

**ManagementLatencyP95High / P99High** — p95/p99 request latency exceeded 2s/5s for 15
minutes straight. First checks: (1) the management dashboard's "DB operation duration
(p95)" panel — slow queries are the most common cause; (2) `waddleai_management_db_up`/
`redis_up` — a degraded-but-not-down dependency often shows as latency before it shows as
an outage; (3) CPU/memory on the pod (`kubectl top pod`) for resource starvation.
Mitigation: same as the burn-rate alerts — rollback, scale out, or flag-off, depending on
what the first checks point to.

**ManagementTargetDown** — Prometheus hasn't scraped `waddleai-management` successfully
for 5 minutes. Means the pod is down, unschedulable, or network-partitioned from
Prometheus (check the Cilium `waddleai-allow-prometheus-scrape` policy if this fires right
after enabling monitoring on a new cluster). First checks: (1) `kubectl get pods -n
waddleai -l app.kubernetes.io/component=management`; (2) `kubectl describe pod` for
scheduling/pull failures; (3) `kubectl logs --previous` if it's crash-looping. Mitigation:
restart/reschedule, or `waddleai.disable-<mechanism>` kill-switch rollback if the previous
deployment introduced a startup regression behind a flag.

**ManagementMetricsAbsent** — No `waddleai-management` target is registered with
Prometheus at all (every replica gone, or the ServiceMonitor/Service label selector
stopped matching after an unrelated chart change). First checks: (1) `kubectl get
servicemonitor -n waddleai waddleai-management -o yaml` and confirm its `selector` matches
the Service's labels; (2) `kubectl get endpoints -n waddleai waddleai-management`; (3)
whether `replicaCount`/the HPA scaled to zero. Mitigation: fix the selector mismatch or
scale back up.

**ManagementDatabaseDown / ManagementRedisDown** — The service's own `/metrics` gauge
(`waddleai_management_db_up`/`redis_up`) reports its Postgres/Redis connection down.
First checks: (1) `kubectl get pods -n waddleai -l app.kubernetes.io/component=postgres`
(or `valkey`); (2) the DB/cache pod's own logs; (3) network policy — a Cilium
default-deny change elsewhere in the namespace can silently cut this path. Mitigation:
restart the dependency pod, or roll back whatever network/credential change coincided.

**ManagementDBPoolSaturated** — **Known dangling reference, never fires today** — see
"Known gap" above; `db_pool_waiting` has no equivalent gauge in management's code.
Once fixed, this alert means requests are queueing for a DB connection. First checks:
(1) `DB_POOL_SIZE` vs. actual
concurrent request volume; (2) slow queries holding connections open (same DB operation
duration panel as the latency alert); (3) a connection leak (pool size growing without
bound). Mitigation: raise `DB_POOL_SIZE` as a stop-gap, fix the slow query/leak as the
real fix.

**ManagementPodRestartingFrequently** — A management pod restarted more than 3 times in
the last hour. First checks: (1) `kubectl logs --previous`; (2) `kubectl describe pod` for
OOMKilled vs. a liveness-probe failure vs. a crash; (3) whether resource limits
(`management.resources.limits`) are undersized for actual usage. Mitigation: raise
limits, or roll back the change that introduced the crash.

**ManagementHPAAtMax** — The management HPA has been at `maxReplicas` for 10 minutes.
Not inherently bad — it means autoscaling is doing its job — but it means there's no more
headroom if load keeps growing. First checks: (1) is this expected traffic growth or a
spike/incident; (2) CPU/memory per pod to confirm it's genuinely compute-bound, not
something else (DB pool, external API) that more replicas won't fix. Mitigation: raise
`maxReplicas`, or address the real bottleneck if more replicas isn't helping.

**ManagementPDBViolated** — Fewer management pods are healthy than the
PodDisruptionBudget's `minAvailable` requires; a voluntary disruption (node drain, rolling
update) is currently blocked, or already failing. First checks: (1) `kubectl get pdb -n
waddleai`; (2) why pods are unhealthy right now (likely another alert here is already
firing — check readiness probes). Mitigation: resolve the underlying pod-health issue
first; the PDB is doing its job by refusing to let disruption make it worse.

**ManagementFeatureFlagEvalErrors / ManagementLicenseCheckErrors** — PostHog flag
evaluations, or license.penguintech.io entitlement checks, are erroring
(`feature_flag_evaluations_total`/`license_checks_total`). Per
critical-rules.md graceful degradation, both fall back to last-known-cached values rather
than crashing — but the cache goes stale if this persists, and Professional/Enterprise
features may incorrectly degrade. First checks: (1) connectivity to
`license.penguintech.io` / the configured PostHog host from inside the cluster (egress
NetworkPolicy, DNS); (2) whether this correlates with a broader outage at those external
services. Mitigation: none client-side beyond waiting out an external outage; if a new
flag/license-check code path is implicated, its own `waddleai.disable-<mechanism>`
kill-switch (named by whichever PR introduced it) reverts to the pre-change behavior.

### Proxy

**ProxyErrorBudgetBurnFast / Mid / Slow / Slowest** — Same shape as management's, scoped
to `service="proxy"`. First checks: (1) `ProxyProviderUnhealthy` — a bad upstream LLM
provider is the single most common proxy error source; (2) the proxy dashboard's error
ratio panel broken down by route (chat completions vs. everything else); (3)
`ProxyDBPoolSaturated`/rate-limit/cache panels for a resource-exhaustion cause. Mitigation:
if provider-caused, the routing engine should already be failing over — confirm it is;
otherwise rollback/scale/flag-off as with management.

**ProxyLatencyP95High / P99High (non-LLM)** — Same as management's latency alerts, scoped
to non-LLM routes only (LLM routes have their own alert below — mixing them would either
hide a real non-LLM regression inside provider noise, or page on every slow provider
response). First checks: same three as management's latency alert, plus whether
`ProxyInflightSaturationHigh` is also firing (queueing behind the concurrency limiter
shows up as latency first).

**ProxyLLMLatencyP95High** — p95 latency on `/v1/chat/completions`/`/v1/messages`/
`/v1/messages/count_tokens` exceeded its own (looser, 30s) budget. First checks: (1)
`waddleai_provider_health` for the provider(s) actually serving this traffic right now
(check the routing engine's current assignment); (2) whether this is one provider or
all of them (one → provider-side issue, fail over; all → likely a proxy-side regression
masquerading as LLM latency). Mitigation: force routing away from the slow provider if
the routing engine isn't already doing so.

**ProxyInflightSaturationHigh** — **Known dangling reference, never fires today** — see
"Known gap" above; the numerator (`waddleai_proxy_inflight_requests`) exists but
`proxy_concurrency_limit` (the configured cap) does not. Once fixed, this alert means
in-flight concurrent requests are above 90% of the proxy's own admission-limiter cap;
expect `ProxyConcurrencyRejectionsHigh` to follow if this persists. First checks: (1) is
this a genuine traffic spike or a slow-downstream pileup (requests not completing, not
more requests arriving); (2) the HPA's current replica count vs. max. Mitigation: scale
out (raise `maxReplicas` if already there), or raise the concurrency cap if the pod has
headroom to handle more.

**ProxyConcurrencyRejectionsHigh** — The proxy has been shedding load (HTTP 429
`overloaded_error`) for 10 minutes straight (`waddleai_proxy_concurrency_rejections_total`).
First checks: same as the saturation alert above — this is usually the
saturation alert's natural consequence once the cap is actually hit, not a separate root
cause. Mitigation: same — scale out or raise the cap, depending on whether the pod itself
has headroom.

**ProxyTargetDown / ProxyMetricsAbsent** — Same meaning as the management equivalents,
scoped to the proxy ServiceMonitor/Service.

**ProxyProviderUnhealthy** — `waddleai_provider_health` is 0 for a specific
`provider`/`endpoint` label pair. First checks: (1) that provider's own status page/API
error responses directly (bypass the proxy); (2) whether the routing engine has already
stopped sending traffic there (check `ProxyErrorBudgetBurn*`/`ProxyLLMLatencyP95High` for
whether this is actually impacting users, or the health check is stale/wrong).
Mitigation: none client-side if it's a genuine upstream outage beyond confirming failover
is working; investigate the health-check logic itself if it disagrees with direct
provider testing.

**ProxyDBPoolSaturated** — **Known dangling reference, never fires today** — same gap
as `ManagementDBPoolSaturated`; see "Known gap" above.

**ProxyRateLimitExceededHigh** — `waddleai_rate_limit_exceeded_total` is incrementing.
First checks: (1) which organization/endpoint is hitting the limit (check the metric's
labels) — a single tenant hammering the API vs. a quota that's simply too low for
legitimate growth; (2) whether this correlates with a known customer scaling up usage.
Mitigation: raise the specific tenant's quota if legitimate; otherwise this is rate
limiting working as intended, not an incident.

**ProxyCacheHitRateLow** — The response cache's hit rate fell below 5% over the last
hour. First checks: (1) whether traffic composition genuinely changed (more unique
prompts, less cacheable); (2) a TTL or cache-key-derivation regression in a recent
deployment (check `shared/cache/response_cache.py` history); (3) whether the cache
backend (Valkey) itself is healthy. Mitigation: roll back a cache-layer regression if
one is implicated; otherwise this may just reflect genuinely diverse traffic and isn't
actionable.

**ProxyPodRestartingFrequently / ProxyHPAAtMax / ProxyPDBViolated** — Same meaning and
first-response as the management equivalents, scoped to proxy.

**ProxyFeatureFlagEvalErrors / ProxyLicenseCheckErrors** — Same meaning and
first-response as the management equivalents.

**ProxyContentFilterAuditorFailOpen / ProxyContentFilterAuditorFailClosed** —
`waddleai_content_filter_fail_total{mode="fail_open"|"fail_closed"}`
(`shared/security/content_filter.py`) is incrementing. Previously a dead/
timed-out/non-200 LLM auditor was silently indistinguishable from a real
ALLOW verdict — neither counter ever fired for that class of failure, this
pair of alerts, and `FilterResult.degraded`/the `content_filter_audit_log.
degraded` column, did not exist. First checks: (1) `waddleai_security_
auditor_duration_seconds{outcome="degraded"}` for the degraded call rate and
latency (a dead endpoint degrades near-instantly; a genuinely overloaded one
degrades near the 10s internal timeout); (2) whether the Ollama endpoint
serving `SECURITY_AUDITOR_MODEL` is actually reachable/healthy; (3) logs for
"LLM auditor degraded" (operational — the mechanism below) vs "LLM auditor
call is broken" (a programming defect in the call path, always fails closed
regardless of the setting below). Mitigation: fix/restore the auditor
endpoint; `SECURITY_AUDITOR_FAIL_MODE` (`open` default / `closed`) governs
whether a degraded auditor call lets content through (`fail_open` firing) or
blocks it (`fail_closed` firing) while the endpoint is down — `closed`
trades availability for safety and is a deliberate operator choice, not a
default recommendation. The opt-out kill switch
`waddleai.disable-auditor-fail-mode-policy` (PostHog, OFF by default)
reverts to the pre-fix legacy behaviour (silently fail open, no counter, no
WARN) if the new telemetry itself needs to be rolled back.

### PenguinCode server

**PenguinCodeErrorBudgetBurnFast / Slow / Slowest** — Same error-budget-burn shape as
management/proxy, over gRPC status codes (`rpc_server_requests_total`) instead of
HTTP status codes. First checks: (1) `kubectl logs -n penguincode
deploy/penguincode-server --tail=200` for the actual gRPC error; (2)
`PenguinCodeDBPoolSaturated`/`PenguinCodeIndexJobFailuresHigh` for a downstream-caused
failure pattern. Mitigation: rollback/scale/flag-off as with the other services.

**PenguinCodeLatencyP95High / P99High** — p95/p99 gRPC latency exceeded 5s/15s
(`rpc_server_duration_seconds`). First checks: (1) the index queue
depth panel — a backed-up indexing pipeline competing for the same DB/CPU resources is
the most likely cause; (2) `PenguinCodeDBPoolSaturated`; (3) whether a specific RPC method
is slow (check the metric's method label) vs. all of them.

**PenguinCodeTargetDown / PenguinCodeMetricsAbsent** — Same meaning as the other
services' equivalents; the `/metrics` route exists today, so this reflects genuine
scrape health, not an expected-on-install false positive.

**PenguinCodeIndexQueueDepthHigh** — More than 50 index-build jobs are pending
(`index_queue_depth`) for 10 minutes straight. First checks: (1)
`PenguinCodeIndexJobFailuresHigh` — a stuck/crashing worker backing up the queue is more
common than genuine overload; (2) whether a bulk re-index was deliberately triggered.
Mitigation: fix the stuck worker, or scale `server.replicas` if this is genuine sustained
load.

**PenguinCodeIndexJobFailuresHigh** — `index_jobs_total{status="failed"}` is
incrementing. First checks: (1) server logs for the parser/extractor exception; (2) pgvector/
graph-store connectivity (`PenguinCodeDBPoolSaturated`, or a direct `psql` check).
Mitigation: fix the failing extractor/parser, or restore DB connectivity.

**PenguinCodeDBPoolSaturated** — Same meaning as management/proxy's, scoped to
penguincode's pgvector connection pool.

**PenguinCodePodRestartingFrequently** — Same meaning as the other services' pod-restart
alerts.

**PenguinCodeHPAAtMax / PenguinCodePDBViolated** — Same meaning and first-response as
the management/proxy equivalents, scoped to the penguincode server's own HPA
(`templates/hpa.yaml`) and PodDisruptionBudget (`templates/pdb.yaml`).

**PenguinCodeFeatureFlagEvalErrors / PenguinCodeLicenseCheckErrors** — **Known dangling
reference, never fire today** — see "Known gap" above; `penguincode_cli/flags/client.py`
never records `feature_flag_evaluations_total`/`license_checks_total`. Once fixed, same
meaning as the other services' flag/license alerts.

## Kill-switch flags as a mitigation

Several alerts above reference "flag-off" as a mitigation for a specific new behavior.
Per `critical-rules.md` Core platform mechanisms, every new operational mechanism in this
audit pass ships behind an opt-out kill-switch flag named `waddleai.disable-<mechanism>`
(proxy/management) or `penguincode.disable-<mechanism>` (penguincode), unseen/OFF =
mechanism on. The exact flag name is defined by whichever PR introduced that mechanism
(e.g. the concurrency limiter, the response cache, the index pipeline) — check that PR's
description or the flag registry in the relevant `shared.utils.feature_flags`/
`flags/client.py` module for the specific key before flipping it. This chart's own
monitoring objects (`ServiceMonitor`/`PrometheusRule`/dashboards) are a cluster-level
Helm `monitoring.enabled` toggle, not a PostHog flag — they're evaluated by the
Prometheus Operator, not application runtime code, so a PostHog flag can't gate them.
