# gRPC Trace Propagation (ops O1-d)

How WaddleAI's internal gRPC calls carry OpenTelemetry trace context and a
correlation id across process boundaries, so a gRPC hop appears as a child
span of the request that triggered it instead of a detached root. Fixes
audit finding O1-d: `CorrelationInterceptor` minted a UUID and never
propagated it, and the management -> proxy/AILB and penguincode CLI ->
server gRPC clients carried zero trace context at all.

## Propagation Contract

Every client interceptor below:

1. Injects the active span's W3C `traceparent` (and, where noted, `baggage`)
   into outgoing gRPC metadata.
2. Reuses an inbound `x-correlation-id` metadata entry if one is already
   present (e.g. forwarded from an upstream HTTP request); mints a fresh
   UUIDv4 only when absent.
3. Opens a CLIENT span named `<service>/<method>` with attributes
   `rpc.system=grpc`, `rpc.service`, `rpc.method`, and — once the call
   completes — `rpc.grpc.status_code`.

Every server interceptor below does the mirror image: extracts
`traceparent`/`baggage` from inbound metadata, attaches it, and opens a
SERVER span as a child of that context (a detached root if no context was
present — e.g. a client that hasn't been instrumented yet).

| Boundary | Client interceptor | Server interceptor | Baggage? |
|---|---|---|---|
| penguincode server <-> any gRPC client using `py_libs` | `py_libs.grpc.interceptors.TracingClientInterceptor` (sync) / `AsyncTracingClientInterceptor` (`grpc.aio`) | `py_libs.grpc.interceptors.CorrelationInterceptor` | Yes (W3C Baggage, key `correlation_id`) |
| management -> proxy AILB module | `services.management.app.grpc.client._TracingClientInterceptor` | n/a (AILB module calls are currently mocked pending proto stub generation) | No — `x-correlation-id` metadata only; management's `shared.observability.tracing` propagator doesn't carry baggage |
| penguincode CLI -> penguincode server | `penguincode_cli.client.tracing_interceptor.TracingClientInterceptor` (`grpc.aio`) | penguincode server-side extract (owned by a sibling task, `penguincode_cli/server/*`) | Yes (W3C Baggage) |
| proxy gRPC server (`WaddleAIServiceServicer`) | n/a — inbound only | `shared.observability.grpc_tracing.TracingServerInterceptor` | Extracts baggage if present; doesn't mint its own |

## Metrics

Every client interceptor records, with bounded labels only
(`service`, `method`, `status_code` — never ids/paths/emails):

- `rpc_client_duration_seconds` (histogram, seconds)
- `rpc_client_requests_total` (counter)

The proxy's server-side interceptor (`shared.observability.grpc_tracing`)
additionally records the inbound counterparts:

- `rpc_server_duration_seconds` (histogram, seconds)
- `rpc_server_requests_total` (counter)

## Feature Flags (opt-out kill switches)

Per-service, following each service's own flag-key convention. Unseen/OFF
keeps propagation ON; flip ON only to fall back to the pre-fix (no
propagation) behavior, e.g. during an incident where the OTel pipeline
itself is implicated.

| Service | Flag key |
|---|---|
| management (AILB client) | `waddleai.disable-grpc-trace-propagation` |
| penguincode CLI client | `penguincode.disable-grpc-trace-propagation` |

`py_libs`'s interceptors and the proxy's server-side interceptor have no
kill switch — they are the shared library primitive and the proxy's only
gRPC server respectively; disabling them has no legacy behavior to fall
back to other than "stop tracing," which is already covered by unsetting
`OTEL_EXPORTER_OTLP_ENDPOINT` (the existing no-op posture, see
`critical-rules.md` Observability).

## Known Gaps

- **Streaming RPCs are not wrapped.** All interceptors above only wrap
  `unary_unary` handlers/calls (matching the pre-existing limitation in
  `py_libs.grpc.interceptors.AuditInterceptor`/`RecoveryInterceptor`).
  Server-streaming/bidi RPCs (e.g. the penguincode CLI's chat/tool-callback
  streams) pass through unwrapped — context is not attached, no span is
  opened. Tracked as follow-up, not blocking: those calls still work, they
  are just untraced today.
- **Management's AILB client carries no baggage**, only the plain
  `x-correlation-id` metadata key, because `shared.observability.tracing`'s
  global propagator is configured for W3C TraceContext only (no baggage
  propagator installed). Extending it is a `shared/` change outside this
  fix's scope.
