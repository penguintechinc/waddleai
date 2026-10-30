# Classification Service

PII/NER classification as its own Deployment (`waddleai/classification`), so the
~1.8Gi spaCy `en_core_web_lg` model lives outside the proxy. Source:
`services/classification/`, chart templates `k8s/helm/waddleai/templates/classification-*.yaml`.
It is the contract the P3 Rust proxy consumes.

## API surface

- **Business API is gRPC only** (port `50051`, TLS). There is no business REST API,
  so **OpenAPI is N/A**.
- REST is ops-only on port `8080`: `/healthz`, `/livez`, `/readyz`, `/metrics`
  (never carries gRPC; separate `-classification-metrics` ClusterIP Service).
- Standard `grpc.health.v1.Health` is served alongside; it mirrors `/readyz`.
- Headless Service (gRPC) + server-side connection aging (`MAX_CONNECTION_AGE` 300s, 30s grace)
  lets a long-lived client re-resolve DNS and rebalance after an HPA scale-out.

## Proto: `waddleai.classification.v1` (frozen)

`proto/waddleai/classification/v1/classification.proto`

| RPC | Purpose |
|---|---|
| `Analyze(AnalyzeRequest) -> AnalyzeResponse` | Detect PII entities; returns **spans only** (offsets, type, scores, action), never entity text |
| `GetEntityConfig(GetEntityConfigRequest) -> GetEntityConfigResponse` | Entity table, so clients can contract-test drift |

Every request carries `api_version` (`"v1"`). Enums are ints; field numbers are never reused.
`AnalyzeRequest` includes `org_public_id` (UUID) and `flag_distinct_id` (P3 must populate it).
Text is analyzed up to 2000 chars (`truncated=true` reports more); only `language="en"` is accepted.

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `GRPC_PORT` / `HTTP_PORT` | `50051` / `8080` | |
| `NER_POOL_WORKERS` | `2` | Process-pool size per pod (RSS sizing assumes 2) |
| `NER_SPACY_MODEL` | `en_core_web_lg` | Model is baked into the image |
| `NER_CACHE_TTL_SECONDS` | `0` (chart: `300`) | `>0` enables the result cache |
| `NER_CACHE_HMAC_KEY` | none | **Required** when the cache is on, >=32 bytes, from a Secret; never defaulted |
| `REDIS_URL` | none | Valkey/Redis cache; a down cache never fails readiness |
| `MAX_TEXT_BYTES` / `MAX_QUEUE_DEPTH` | `1048576` / `64` | Overload yields a degraded response |
| `JWT_ISSUER` / `JWT_AUDIENCE` / `JWKS_URL` | none | Bearer-JWT trust anchor (see below) |
| `SPIFFE_ALLOWED_IDS` | empty | `spiffe://` allow-list; enables mTLS and requires a client CA |
| `GRPC_TLS_CERT_FILE` / `GRPC_TLS_KEY_FILE` / `GRPC_TLS_CLIENT_CA_FILE` | none | Server TLS / mTLS material |
| `SECURITY_AUDITOR_FAIL_MODE` | `open` | `open` or `closed` (per-request `fail_mode` overrides) |
| `ALLOW_INSECURE_GRPC` | unset | **Dev/alpha only**: `1` permits plaintext gRPC |
| `OTEL_*` | none | Standard OTel env vars; OTLP destination is never hard-coded |

## Fail-mode and degraded semantics

- NER failure (pool crash, missing model, overload, timeout) is **signaled**: the response has
  `degraded=true` + `degraded_reason`, a counter increments and a rate-limited WARN is logged.
- `OPEN` (default): degraded responses return what was found; the caller proceeds.
- `CLOSED`: degraded responses set `block_recommended=true`; the caller must block.
- Kill-switch: PostHog flag `waddleai.disable-auditor-fail-mode-policy` (opt-out; unseen or
  unreachable = policy ON; ON = legacy silent path).
- A missing model fails **readiness** (`/readyz` 503) rather than silently disabling NER.

## TLS / mTLS (fail-closed)

The service refuses to start without `GRPC_TLS_CERT_FILE` + `GRPC_TLS_KEY_FILE` unless
`ALLOW_INSECURE_GRPC=1` (alpha only). A half-configured pair, or `SPIFFE_ALLOWED_IDS`
without a server cert and client CA, also fails startup. Callers present a bearer JWT with the
classification scope, or an allow-listed SPIFFE mTLS identity.

!!! danger "PROD-DEPLOY REQUIREMENT: JWT trust anchor"
    The operator/release pipeline **MUST supply `classification.jwt.issuer` and
    `classification.jwt.jwksUrl`** (or `classification.jwt.host`, which derives both). The
    chart **fails closed**: with the service enabled and no trust anchor the render fails.
    There is deliberately **no vendor default**, and the anchor is never inherited from
    `ingress.hosts`/`httproute.host`. `jwksUrl` must be `https:`; the same host feeds the
    Cilium egress allow-list. `values.yaml` ships `classification.enabled: false`; each
    environment overlay enables it and supplies the anchor.

## Tests and verification

| What | How |
|---|---|
| Image real-model test | `NER_MODEL_REQUIRED=1 pytest -m ner_model` (in-image; fails, never skips, when set); `make test-classification-real` |
| Live smoke | `CLASSIFICATION_SMOKE_ENDPOINT=host:50051 make smoke-test-classification` (skips without the variable; with it, nothing skips). See `tests/smoke/test_classification_smoke.py` for the other `CLASSIFICATION_SMOKE_*` variables |
| Load | `make loadtest-classification` prints `p99_analyze_latency_warm_cache_miss_ms`; exit 2 if over the budget. The budget is an owner decision; the script never relaxes it |

Alpha is amd64-only for now. The live smoke checks: authenticated Analyze returns a PERSON span,
unauthenticated is rejected, `/readyz` flips 503 when the pool is killed and recovers, telemetry
counts (logs, metric points, histograms) are non-zero, and HPA scale-out rebalances a long-lived client.
