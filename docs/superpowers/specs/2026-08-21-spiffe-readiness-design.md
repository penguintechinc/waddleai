# SPIFFE Readiness — Design

**Status**: draft for review
**Date**: 2026-08-21

## Problem

SPIFFE/SPIRE is currently docs-only in this repo: zero implementation in
`shared/`, `proxy/`, `services/`, or `k8s/` (confirmed by grep across all
four). The house standard (`security.md` "Service-to-Service Auth",
`penguintech.md` "SPIFFE Identity") requires every service to be
SPIFFE-*ready* — accepts mTLS/X.509-SVID as first-class identity regardless
of whether SPIRE is deployed — with short-lived signed JWTs (JWS, nested JWE
for sensitive claims) as the fallback, and every inter-service call
authenticated regardless of transport. Today none of WaddleAI's inter-service
calls meet that bar:

| Call | Mechanism today | Evidence |
|---|---|---|
| Client → proxy gRPC (port 50051) | Pre-shared Bearer token (`PROXY_GRPC_AUTH_TOKEN`), checked by `GrpcAuthInterceptor`; fail-closed only in the sense that an unset token rejects *everything*, not that it's absent | `proxy/apps/proxy_server/grpc_server.py:43-75,403-443`; wired in `proxy/apps/proxy_server/main.py:500-522` |
| gRPC transport itself | Plaintext — `add_insecure_port`, no TLS at all, static token is the *only* protection | `proxy/apps/proxy_server/grpc_server.py:440`; same pattern in the shared helper `shared/py_libs/py_libs/http/../grpc/server.py:51,152` |
| Proxy → management (REST) | No auth found — only an unauthenticated `GET /healthz` | `proxy/apps/proxy_server/main.py:263,406` (no other `management_server_url` call site exists in that file) |
| AIProxy → external bare-metal Ollama/llama.cpp fleet, alpha/dev | Shared bearer token (`FLEET_EXTERNAL_TOKEN`) injected by an nginx sidecar | `k8s/helm/waddleai/templates/fleet-external-mtls.yaml:44-70` |
| AIProxy → external bare-metal fleet, beta/prod default | Already client-cert mTLS via cert-manager (single internal CA, plain CN — **not** SPIFFE-shaped: no URI SAN, no trust-domain semantics, no short TTL rotation tied to workload identity) | `k8s/helm/waddleai/templates/fleet-external-mtls.yaml:19-42` |
| Fleet cloud connectors (Vertex AI, Bedrock, exo) | Provider-native creds (GCP JWT-bearer OAuth2, AWS session creds, configured bearer) | `shared/fleet/vertex_ai.py:123-159`, `shared/fleet/bedrock.py:121-122`, `shared/fleet/exo.py:109-111` — **out of scope**, these are third-party APIs, not PenguinTech inter-service calls |
| MCP gateway outbound auth (Jira etc.) | Per-user OAuth2 / static header, user-delegated | `shared/mcp/gateway/auth.py:1-40` — **out of scope**, this is the credential-reference-injection feature's territory (sibling spec), not inter-PenguinTech-service auth |

In-app TLS/cert handling is essentially absent: the only `verify=` knob found
is an outbound httpx flag (`shared/py_libs/py_libs/http/client.py:105`); zero
hits for `SSLContext`/`load_cert_chain`/`CERT_REQUIRED` anywhere in
`shared/`, `proxy/`, `services/`. All TLS termination today happens at
ingress (cert-manager + Let's Encrypt, `k8s/helm/waddleai/values-beta.yaml:108-126`)
or in the fleet-external mTLS chart above. No service accepts a client
certificate as identity anywhere in the codebase.

This blocks the sibling **credential-reference-injection** design
(`docs/superpowers/specs/2026-08-21-credential-reference-injection-design.md`,
"Relationship to SPIFFE/SPIRE" section): the proxy's call to
skauswatch/Vault for secret resolution is exactly the kind of inter-service
call that must not be protected by a new static shared secret. That spec
explicitly defers to this one and expects SPIFFE-readiness to land before or
alongside it.

## Goals

- Every WaddleAI service (proxy, management, webui) accepts an X.509-SVID
  over mTLS as first-class identity, with short-lived JWT-SVID / OIDC
  machine JWT as fallback — never a static token.
- Retire `PROXY_GRPC_AUTH_TOKEN` and `FLEET_EXTERNAL_TOKEN` (alpha/dev mode)
  as the *only* protection on a call; a static token may remain as a
  break-glass fallback but never as the sole credential once the identity
  middleware ships.
- Close the proxy→management authentication gap (currently nothing).
- The service-side contract must be identical regardless of which identity
  issuer stands behind it, so the org can move from "no SPIRE here yet" to
  "skauswatch's shared SPIRE" without touching service code twice.
- Reuse `penguin-aaa`'s existing SPIFFE support; do not reimplement identity
  validation in `shared/`.

## Non-goals

- Deploying a new, separate SPIRE control plane for WaddleAI — skauswatch
  already operates one for the `penguintech.io` trust domain on the shared
  dal2 clusters WaddleAI runs on.
- Re-architecting the credential-reference-injection feature itself (covered
  by its own spec).
- Client-facing user auth (OIDC/JWT for end users) — unaffected, this is
  service-to-service only.
- Encrypting datastore connections (Postgres/Valkey) under SVIDs — those stay
  on per-service DB-account TLS per `backend-database.md`; only *app* service
  identity is in scope here.

## Option A: Lightweight intra-product issuer

WaddleAI mints and rotates its own short-lived X.509 client certs for
proxy/management/webui, using cert-manager (already deployed,
`k8s/helm/waddleai/values-beta.yaml:108`) as the CA, with URI SANs shaped
like SPIFFE IDs (`spiffe://penguintech.io/<env>/waddleai-<service>`) even
though no real SPIRE server issues them.

- **What it issues**: X.509 certs with a SPIFFE-shaped URI SAN, via
  cert-manager `Certificate` resources — the same primitive already used for
  `waddleai-fleet-external-client-tls` (`fleet-external-mtls.yaml:19-42`).
- **Rotation**: cert-manager's own renewal (`renewBefore`), same pattern as
  the existing fleet client cert.
- **CA key**: lives in the cluster's cert-manager `ClusterIssuer`/`Issuer`
  secret — WaddleAI's own, not shared with any other product.
- **Blast radius**: a compromised CA key only affects WaddleAI's own trust
  boundary; but this is also the weakness — it does **not** interoperate
  with any other PenguinTech service's identity, and it duplicates
  infrastructure the org already built (skauswatch's SPIRE) rather than
  federating with it. Cross-product calls (e.g. WaddleAI → skauswatch for
  credential resolution) would need a second, separate trust relationship
  bolted on top, defeating the point of a single `penguintech.io` trust
  domain.

## Option B: Full SPIRE, managed by skauswatch

skauswatch already runs a real SPIRE deployment for the `penguintech.io`
trust domain: one org-wide root SPIRE server (deployed outside skauswatch's
own namespace, `root.example.yml` — "copy it into wherever k8s-ops
designates as the shared root cluster/namespace") plus one child SPIRE
server per cluster, including **dal2-beta and dal2-gamma** — the same shared
cluster contexts WaddleAI deploys into
(`/home/penguin/code/skauswatch/k8s/helm/spire/README.md`, topology diagram;
`values.yaml:10` trust domain `penguintech.io`). Node attestation is
`k8s_psat` (bare-metal dal2, no IRSA/IMDS chain) — the same attestor
WaddleAI's own nodes would use.

- **What's missing today**: the child servers' registration entries
  (`values.yaml:374-419`) only cover `namespace: skauswatch`. WaddleAI
  workloads are not registered. This is a **configuration change on an
  already-running shared control plane**, not a new deployment — exactly the
  "config/infra change, not a rewrite" framing in `security.md`.
- **Agent sockets / Workload API**: `spire-agent` runs as a DaemonSet per
  node on dal2-beta/gamma already; WaddleAI pods would mount the same
  `agent.sock` (hostPath or CSI driver, per skauswatch's chart) and fetch
  their SVID via the standard Workload API — no new agent to run.
- **Client library**: `py-spiffe>=0.8.0`, already declared as `penguin-aaa`'s
  optional `spiffe` extra (`python-aaa/pyproject.toml:44-45`) but not yet
  wired to any transport in `penguin_aaa.authn.spiffe`
  (`python-aaa/src/penguin_aaa/authn/spiffe.py` docstring: "In production
  this is paired with a SPIFFE Workload API client... This class handles the
  identity-comparison logic independently of the transport layer" — i.e. the
  Workload-API-fetch-and-mTLS-wire-up doesn't exist yet anywhere in
  `penguin-aaa`, WaddleAI would be the first consumer to need it built).
- **Who deploys**: skauswatch's own team/agent owns the SPIRE Helm chart and
  registration entries; WaddleAI only requests entries be added for its
  `waddleai-proxy`/`waddleai-management`/`waddleai-webui` service accounts —
  cross-repo coordination, not something this repo can self-serve.
- **Blast radius**: shared trust domain — a compromised WaddleAI SVID is
  scoped by SPIRE's registration entry (selector-bound to a specific
  namespace + service account), same isolation any other tenant on that
  SPIRE deployment gets. A compromised *root* CA is catastrophic org-wide,
  but that risk already exists independent of WaddleAI's decision here.

## Option C: Cilium mutual authentication as substrate

Cilium's mutual-authentication feature enforces mTLS at the network layer
using SPIFFE identities under the hood, and this repo already has Cilium as
the assumed CNI with a working `CiliumNetworkPolicy` set
(`k8s/helm/waddleai/templates/cilium-network-policy.yaml`,
`cilium-configmap.yaml`, `cilium-rbac.yaml`). However, grep for
`mutual|mTLS|spire|spiffe` in the existing `cilium-network-policy.yaml`
returns nothing — it is not configured, and more importantly Cilium's
mutual-auth mode itself *requires* a SPIFFE-compatible identity provider
(SPIRE) behind it — it is not a substitute for Option B, it is a
network-layer enforcement point that sits *on top of* whichever SPIRE
deployment answers Option B. It also only proves connection-level identity
to Cilium's own proxy/socket layer; it does not hand the application process
an SVID it can use to mint a JWT-SVID with WaddleAI-specific claims (scope,
tenant) or verify a peer's identity at the app layer for authorization
decisions (the service-side contract below needs the SVID *in the app*, not
just at the network hop). Treat Option C as a defense-in-depth layer to add
once Option B is live, not as an alternative to it.

## Recommendation

**Option B** (skauswatch's shared SPIRE, WaddleAI workloads registered
against the existing dal2-beta/dal2-gamma child servers), with the
service-side contract below built so it degrades to OIDC machine JWTs
wherever a given environment's SPIRE registration isn't live yet (e.g. local
alpha dev without a workload API socket mounted). This avoids standing up
parallel CA infrastructure (Option A), matches the org's one-trust-domain
principle, and reuses real, already-hardened infra (skauswatch's chart shows
production rollout history: rootless agent images, PostgreSQL-backed
datastore with verified TLS, hardened alpha deployment). Cilium mutual auth
(Option C) is a good follow-on once B is live, not a precondition.

## Service-side contract (identical across options)

This is the part that ships in WaddleAI's own code and does not change based
on which issuer answers Option A/B.

**Identity middleware, checked in order**:
1. **X.509-SVID over mTLS** — the server's TLS listener requests (and, once
   SPIRE is registered, requires) a client cert; the middleware extracts the
   URI SAN, validates it's a well-formed `spiffe://penguintech.io/...` ID
   (reuse `penguin_aaa.authn.validators.validate_spiffe_id`, already
   published — do not reimplement string/format validation in `shared/`),
   and checks it against a per-endpoint allowlist via
   `penguin_aaa.authn.spiffe.SPIFFEAuthenticator` (already published —
   `python-aaa/src/penguin_aaa/authn/spiffe.py:26-99`). WaddleAI's
   contribution is the Workload-API fetch + mTLS wiring this class's
   docstring says doesn't exist yet (see Dependencies below).
2. **Fallback: short-lived JWT-SVID / OIDC machine JWT** — 1h max, JWS
   always, nested JWE (sign-then-encrypt, RFC 8725 §3.4) when claims carry
   sensitive data, per the `building-apis` skill's canonical pattern
   (`~/.claude/skills/building-apis/SKILL.md:14,38-61`). Used wherever mTLS
   isn't available (e.g. a REST hop through a load balancer that terminates
   TLS before it reaches the pod).
3. **Never**: a static pre-shared token as the sole credential. A static
   token may exist transiently as a documented break-glass fallback during
   migration (see below) but is never the only check once this ships.

**SPIFFE ID ↔ scope mapping**: the SVID's path segment after
`waddleai-<service>` maps 1:1 to an OIDC scope bundle already defined by
`security.md`'s scope-bundle convention (`*:read`, `*:write`, etc.) — e.g.
`waddleai-proxy` gets `routing:read routing:write fleet:invoke`,
`waddleai-management` gets `config:read config:write routing:admin`. No new
bundle taxonomy; this reuses the existing scope table, just keyed by SPIFFE
ID instead of a JWT `sub`.

**Reserved SPIFFE IDs** (`spiffe://penguintech.io/<env>/<id>`, env ∈
`alpha|beta|gamma|prod`):

| ID | Deployed as | Replaces |
|---|---|---|
| `waddleai-proxy` | `proxy-deployment.yaml` | `PROXY_GRPC_AUTH_TOKEN` as gRPC server identity check; `management_server_url` calls gain an outbound identity |
| `waddleai-management` | `management-deployment.yaml` | client identity for calls into proxy's gRPC service |
| `waddleai-webui` | `webui-deployment.yaml` | (webui has no inter-service static credential today; gets an ID for its own outbound calls to management's API for parity/future use) |
| `waddleai-aiproxy-fleet-client` | proxy pod, fleet-egress identity | `FLEET_EXTERNAL_TOKEN` (alpha/dev token mode) and the non-SPIFFE cert-manager client cert (beta/prod mtls mode, `fleet-external-mtls.yaml:19-42`) |

`services/penguincode` is a developer CLI, not a deployed service — no ID
reserved; if it ever becomes a persistent daemon (it already references port
50051 at `services/penguincode/penguincode_cli/main.py:75`) it needs one
then.

**gRPC server (port 50051)**: `grpc.aio.server` currently binds
`add_insecure_port` unconditionally (`grpc_server.py:440`). Add
`add_secure_port` using `grpc.ssl_server_credentials` with
`require_client_auth=True`, sourced from the SVID cert/key pair delivered by
the Workload API (mounted via the SPIRE CSI driver/agent socket, same as
skauswatch's own services). The existing `GrpcAuthInterceptor`
(`grpc_server.py:43-75`) becomes the *fallback* path — checked only when the
call arrives without a peer cert, and then requires the JWT-SVID/OIDC
machine JWT instead of the static Bearer token.

**REST/Quart/hypercorn (management, webui)**: hypercorn's `Config` gains
`ca_certs` + `verify_mode = ssl.CERT_REQUIRED` for the mTLS listener (today:
zero SSLContext usage anywhere — this is new). A Quart `before_request`
middleware mirrors the gRPC interceptor: peer cert SAN first, JWT
`Authorization` header fallback. Since hypercorn terminates TLS itself here
(unlike the ingress-terminated public listener), this is a second, internal
listener/port dedicated to service-to-service calls — public traffic keeps
going through the existing ingress + cert-manager path unchanged.

**Outbound clients**: proxy's httpx/aiohttp clients (already gated by
`verify_ssl`, `shared/py_libs/py_libs/http/client.py:105`) add a client-cert
tuple sourced from the same Workload-API-delivered SVID; the gRPC client
side (currently unauthenticated — no channel credentials found in
`shared/routing/grpc_adapter.py`) adds `grpc.ssl_channel_credentials` with
the SVID as client cert.

**Config surface**:
- Env vars: `SPIFFE_WORKLOAD_SOCKET` (default `/run/spire/agent.sock`),
  `SPIFFE_TRUST_DOMAIN` (default `penguintech.io`), `SPIFFE_ENABLED`
  (bool, default `false` until an environment has a live registration —
  fail open to the JWT fallback, never fail open to no auth at all),
  `PROXY_GRPC_AUTH_TOKEN` / `FLEET_EXTERNAL_TOKEN` retained as
  deprecated-but-honored fallback names during migration.
- Helm values: `spiffe.enabled`, `spiffe.workloadSocketHostPath` (CSI driver
  or hostPath mount, matching whichever mechanism skauswatch's chart uses),
  `spiffe.trustDomain`, per-service `spiffe.allowedPeers` list (feeds
  `SPIFFEConfig.allowed_ids`).

## Migration sequence

1. **Coordinate with skauswatch**: request `waddleai-proxy`,
   `waddleai-management`, `waddleai-webui` registration entries on the
   dal2-beta/dal2-gamma child SPIRE servers (cross-repo ask, not a WaddleAI
   PR). Interim: none needed yet — no code changes ship before this exists
   to test against.
2. **penguin-aaa**: contribute the Workload-API-fetch + mTLS wiring that
   `SPIFFEAuthenticator`'s docstring describes but doesn't implement (see
   Dependencies). Interim: WaddleAI cannot build the real middleware without
   this; blocks step 3.
3. **proxy gRPC server** (`grpc_server.py`): add the mTLS listener +
   SVID-based interceptor, `PROXY_GRPC_AUTH_TOKEN` becomes fallback-only.
   Interim credential while this rolls out: the existing static token stays
   the *only* check, unchanged — no regression, just not yet improved.
4. **proxy → management REST**: add outbound SVID + JWT fallback to the one
   real inter-service call surface (currently none beyond `/healthz`, which
   stays open/unauthenticated as a standard liveness probe). Interim: none —
   this is a net-new auth addition, not a credential swap.
5. **AIProxy → external fleet**: fold the existing beta/prod cert-manager
   client cert (`fleet-external-mtls.yaml:19-42`) into a proper SVID (URI
   SAN instead of plain CN); alpha/dev `FLEET_EXTERNAL_TOKEN` mode becomes
   fallback-only. Interim: token mode is explicitly alpha/dev-only already
   per the chart's own comments — lowest urgency.
6. **Retire fallback tokens** once every environment has confirmed SVID
   issuance is live (all four env files) — remove
   `PROXY_GRPC_AUTH_TOKEN`/`FLEET_EXTERNAL_TOKEN` fallback code paths
   entirely, not just stop using them.

## Dependencies on penguin-aaa

**Contribute upstream** (belongs in `penguin-aaa`, benefits every PenguinTech
service, not WaddleAI-specific):
- The actual `py-spiffe` Workload API client wiring — fetching the SVID from
  `workload_socket`, watching for rotation, exposing it as a cert/key pair
  usable by both grpc and hypercorn/quart.
- A generic ASGI/Quart middleware analogous to the existing
  `penguin_aaa.middleware.asgi` module that checks peer SVID first, falls
  back to JWT — so every Python service gets this contract by importing one
  middleware, not reimplementing the order-of-checks logic.
- A grpc server-side interceptor equivalent (mirrors the ASGI middleware,
  for the gRPC transport).

**Stays product-local** (WaddleAI-specific, not general enough for
penguin-aaa):
- The `waddleai-<service>` ID list and its scope-bundle mapping.
- Helm wiring for the workload socket mount and the internal mTLS listener
  port.
- The `PROXY_GRPC_AUTH_TOKEN`/`FLEET_EXTERNAL_TOKEN` fallback-and-retire
  sequence, since those credentials are WaddleAI-specific.

## Test strategy

- **Unit**: generate self-signed SVID-shaped certs at test time with
  `cryptography` (already pinned, `requirements.in:38`) — X.509 cert with a
  `spiffe://penguintech.io/test/...` URI SAN, short validity. Exercise the
  middleware's accept/reject paths without a real SPIRE agent.
- **Contract tests**: one shared test module asserting the middleware
  behaves identically whether the peer identity arrives via mTLS SVID or via
  JWT-SVID fallback — same scope resolution, same rejection behavior for an
  unregistered/expired identity. This is what proves Option A/B/C
  interchangeability at the service-side contract layer.
- **E2E**: SPIRE in a kind/MicroK8s cluster (mirrors `local-alpha` context),
  standalone topology (matches skauswatch's own `alpha.yml` "standalone,
  self-signed, local dev only" mode) — register WaddleAI's three service
  IDs, deploy proxy+management, confirm gRPC and REST calls succeed with
  mTLS and fail closed when the peer cert is absent or wrong trust domain.

## Open questions for the owner

1. Confirm with skauswatch's owner: is registering WaddleAI workloads on the
   existing dal2-beta/dal2-gamma child SPIRE servers (Option B) acceptable,
   or does skauswatch's team want WaddleAI to run its own child server under
   the same root instead?
2. Does the `penguin-aaa` Workload-API-fetch + mTLS wiring get built as part
   of this migration (WaddleAI contributes it upstream first), or does
   WaddleAI build it locally first and upstream it after proving it out?
3. Priority/timing: does this need to land before the credential-reference-
   injection feature starts implementation, or can they land as parallel
   PRs merging into the same release branch?
4. Local alpha dev without a mounted workload socket — is the JWT-SVID/OIDC
   machine JWT fallback sufficient, or does alpha need its own standalone
   SPIRE (mirroring skauswatch's own `alpha.yml` pattern) for parity?
