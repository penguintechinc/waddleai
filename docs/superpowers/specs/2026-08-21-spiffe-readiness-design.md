# SPIFFE Readiness — Design

**Status**: decisions recorded 2026-08-21 — ready for planning
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
hits for `SSLContext`/`load_cert_chain`/`CERT_REQUIRED` in `shared/`,
`proxy/`, `services/`. TLS termination happens only at ingress or the
fleet-external mTLS chart above; no service accepts a client cert as
identity anywhere in the codebase.

This blocks the sibling **credential-reference-injection** design
(`docs/superpowers/specs/2026-08-21-credential-reference-injection-design.md`,
"Relationship to SPIFFE/SPIRE" section): the proxy's call to
skauswatch/Vault for secret resolution is exactly the kind of inter-service
call that must not be protected by a new static shared secret. That spec
explicitly defers to this one for its outbound auth path; Decision 3 below
resolves the sequencing — the two ship as parallel PRs via a hard interface
seam (see Migration sequence), not a hard block in either direction.

## Goals

- Every WaddleAI service (proxy, management, webui) accepts **both**
  X.509-SVID over mTLS and JWT-SVID as first-class identity mechanisms —
  not one preferred and the other a fallback (Decision 4) — with a
  short-lived OIDC machine JWT as the true fallback only where no SPIRE
  identity is available at all, and never a static token.
- Retire `PROXY_GRPC_AUTH_TOKEN` and `FLEET_EXTERNAL_TOKEN` (alpha/dev mode)
  as the *only* protection on a call; a static token may remain as a
  break-glass fallback but never as the sole credential once the identity
  middleware ships.
- Close the proxy→management authentication gap (currently nothing).
- The service-side contract must be identical regardless of which identity
  issuer stands behind it or which `spire.mode` deploys it (Decision 1: own
  vs shared), so the org can move between them without touching service code.
- Reuse `penguin-aaa`'s existing SPIFFE support; do not reimplement identity
  validation in `shared/`.

## Non-goals

- A SPIRE control plane outside the `penguintech.io` trust domain — both
  supported modes (Decision 1) join or reuse that root; never a
  standalone/unfederated CA.
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

- **What it issues**: X.509 certs with a SPIFFE-shaped URI SAN via
  cert-manager `Certificate` resources — same primitive as
  `waddleai-fleet-external-client-tls` (`fleet-external-mtls.yaml:19-42`),
  rotated via cert-manager `renewBefore`, CA key in WaddleAI's own
  `ClusterIssuer`/`Issuer` secret.
- **Blast radius**: contained to WaddleAI's boundary, but doesn't
  interoperate with any other PenguinTech service's identity and duplicates
  infra the org already runs (skauswatch's SPIRE). **Superseded by
  Decision 1**: the approved "own" mode is a real child SPIRE server joined
  to the `penguintech.io` root, not this cert-manager substitute — Option A
  stays rejected.

## Option B: Full SPIRE — own child server or skauswatch-shared child servers

skauswatch already runs a real SPIRE deployment for the `penguintech.io`
trust domain: one org-wide root server (`root.example.yml`, location TBD by
k8s-ops) plus one child SPIRE server per cluster, including **dal2-beta and
dal2-gamma**
(`/home/penguin/code/skauswatch/k8s/helm/spire/README.md`, topology diagram;
`values.yaml:10` trust domain `penguintech.io`). Node attestation is
`k8s_psat` (bare-metal dal2) — the same attestor WaddleAI's own nodes would
use either way.

**Decision 1 (2026-08-21)** requires both child-server topologies be
supported, selected by a Helm values toggle:

| Mode | What's deployed | Trust relationship |
|---|---|---|
| `own` (**default**) | WaddleAI's own child SPIRE server + `spire-agent` DaemonSet, deployed by this repo's own chart | Child server does an upstream-authority join to the shared `penguintech.io` root — same root as skauswatch's children, own registration entries |
| `shared` (**docs-recommended** where skauswatch is already deployed in that cluster) | No new server — WaddleAI pods mount skauswatch's existing `agent.sock` (hostPath/CSI, per skauswatch's chart) | Registration entries (`waddleai-proxy`/`waddleai-management`/`waddleai-webui`) handed to skauswatch's team to add to their existing child servers on dal2-beta/dal2-gamma — cross-repo ask, not self-serve |

Either mode fetches the SVID via the same standard Workload API — **the
service-side contract below is identical regardless of mode**, which is the
entire point of the toggle.

- **Client library**: `py-spiffe>=0.8.0`, already declared as `penguin-aaa`'s
  optional `spiffe` extra (`python-aaa/pyproject.toml:44-45`) but not yet
  wired to any transport (see Dependencies below — **Decision 2**: this is
  built upstream in penguin-aaa first, not locally).
- **Blast radius**: shared trust domain either way — a compromised WaddleAI
  SVID is scoped by its registration entry (namespace + service account);
  a compromised *root* CA is catastrophic org-wide regardless of mode.

## Option C: Cilium mutual authentication as substrate

Cilium's mutual-authentication feature enforces mTLS at the network layer
using SPIFFE identities, and this repo already assumes Cilium as CNI with a
working `CiliumNetworkPolicy` set
(`k8s/helm/waddleai/templates/cilium-network-policy.yaml`,
`cilium-configmap.yaml`, `cilium-rbac.yaml`) — but grep for
`mutual|mTLS|spire|spiffe` there returns nothing (unconfigured), and
Cilium's mutual-auth mode itself *requires* a SPIFFE identity provider
(SPIRE) behind it; it sits *on top of* whichever SPIRE mode answers
Option B, not a substitute. It also only proves connection-level identity
at Cilium's proxy/socket layer — it doesn't hand the app process an SVID to
mint a JWT-SVID with WaddleAI claims or authorize at the app layer, which
the service-side contract needs. Treat Option C as defense-in-depth once a
SPIRE mode is live, not an alternative.

## Recommendation

**Option B, both modes, behind a values toggle** — not one fixed topology.
Per Decision 1, `spire.mode` defaults to `own` (WaddleAI runs its own child
SPIRE server under the `penguintech.io` root, no cross-repo coordination
required to ship), while the chart's values comments and this doc recommend
`shared` (skauswatch's existing dal2-beta/dal2-gamma child servers) wherever
skauswatch is already deployed in that cluster, avoiding duplicate SPIRE
infrastructure. Both modes hand the contract below the same Workload API
socket shape, so the org flips the toggle per-environment without touching
application code.

The contract degrades to a short-lived OIDC machine JWT only where *no*
SPIRE identity is available yet (e.g. local alpha without a workload socket
mounted) — never a preference over SVIDs once either mode is live (Decision
4: X.509-SVID and JWT-SVID are both first-class, not preferred/fallback).
This avoids standing up parallel, unfederated CA infrastructure (Option A,
rejected), matches the org's one-trust-domain principle, and reuses
skauswatch's already-hardened SPIRE chart. Cilium mutual auth (Option C) is
a good follow-on once a SPIRE mode is live, not a precondition.

## Service-side contract (identical across options)

This is the part that ships in WaddleAI's own code and does not change
based on `spire.mode` or the credential-reference-injection interim (see
Migration sequence).

**Both identity mechanisms are first-class (Decision 4)** — never "mTLS
preferred, JWT fallback." Which one a given call presents depends on
transport and environment, not app-level preference:

1. **X.509-SVID over mTLS** — the server's TLS listener requests (and, once
   a SPIRE mode is live, requires) a client cert; the middleware extracts
   the URI SAN, validates it's a well-formed `spiffe://penguintech.io/...`
   ID (reuse `penguin_aaa.authn.validators.validate_spiffe_id` — do not
   reimplement string/format validation in `shared/`), and checks it
   against a per-endpoint allowlist via
   `penguin_aaa.authn.spiffe.SPIFFEAuthenticator` (already published —
   `python-aaa/src/penguin_aaa/authn/spiffe.py:26-99`).
2. **JWT-SVID** — signature verified against the trust bundle's JWKS
   (`kid` rotation tolerated), plus `aud`/`exp`; authorized on `sub`. Alpha
   issues JWT-SVID by default (Decision 4: a genuine exercise of that path,
   not a degraded mode), and it's used anywhere mTLS isn't available (e.g.
   a REST hop through a TLS-terminating load balancer).
3. **True fallback: short-lived OIDC machine JWT** — 1h max, JWS always,
   nested JWE (sign-then-encrypt, RFC 8725 §3.4) for sensitive claims, per
   the `building-apis` skill's canonical pattern. Only where *no* SPIRE
   identity exists yet.
4. **Never**: a static pre-shared token as the sole credential. May exist
   transiently as a documented break-glass fallback during migration, never
   the only check once this ships.

**Validation depth — trust root / intermediate CA level, never leaf pinning
(Decision 4).** X.509-SVIDs validate the full chain to the SPIRE trust
bundle (rotating intermediates tolerated), then authorize on the SPIFFE ID
in the URI SAN. JWT-SVIDs validate the signature against the bundle's JWKS
(`kid` rotation tolerated) plus `aud`/`exp`, then authorize on `sub`.
**Forbidden**: pinning a leaf certificate fingerprint, pinning a single
`kid`, or allowlisting by certificate serial number — any of these breaks
the moment SPIRE rotates a leaf/intermediate/key, which it does routinely
and by design.

**SPIFFE ID ↔ scope mapping**: the SVID's path segment after
`waddleai-<service>` maps 1:1 to an OIDC scope bundle already defined by
`security.md`'s scope-bundle convention (`*:read`, `*:write`, etc.) — e.g.
`waddleai-proxy` gets `routing:read routing:write fleet:invoke`,
`waddleai-management` gets `config:read config:write routing:admin`. No new
bundle taxonomy; this reuses the existing scope table, just keyed by SPIFFE
ID instead of a JWT `sub`.

**Reserved SPIFFE IDs** (`spiffe://penguintech.io/<env>/<id>`, env ∈
`alpha|beta|gamma|prod`; per Decision 4, alpha issues JWT-SVID by default,
beta/gamma exercise both mechanisms):

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
`add_secure_port` via `grpc.ssl_server_credentials(require_client_auth=True)`,
sourced from the Workload-API-delivered SVID (same mount point in either
`spire.mode`). `GrpcAuthInterceptor` (`grpc_server.py:43-75`) becomes the
path used when a call arrives without a peer cert — all of alpha by
default, or wherever TLS terminates upstream — requiring a JWT-SVID
(preferred) or OIDC machine JWT instead of the static Bearer token.

**REST/Quart/hypercorn (management, webui)**: hypercorn's `Config` gains
`ca_certs` + `verify_mode = ssl.CERT_REQUIRED` for a new internal mTLS
listener (today: no SSLContext usage anywhere). A Quart `before_request`
middleware mirrors the gRPC interceptor: peer cert SAN when mTLS is in use,
JWT-SVID/OIDC machine JWT via `Authorization` header otherwise — both
first-class (Decision 4). Public traffic keeps using the existing ingress +
cert-manager path unchanged; this is a second, internal service-to-service
listener/port.

**Outbound clients**: proxy's httpx/aiohttp clients (`verify_ssl`,
`shared/py_libs/py_libs/http/client.py:105`) add a client-cert tuple from
the SVID, or a JWT-SVID/OIDC bearer header where mTLS isn't the transport;
the gRPC client (currently unauthenticated, `shared/routing/grpc_adapter.py`)
adds `grpc.ssl_channel_credentials` with the SVID as client cert.

**Config surface**:
- Env vars: `SPIFFE_WORKLOAD_SOCKET` (default `/run/spire/agent.sock`),
  `SPIFFE_TRUST_DOMAIN` (default `penguintech.io`), `SPIFFE_ENABLED` (bool,
  default `false` until a mode is live — fail open to the JWT fallback,
  never to no auth at all), `PROXY_GRPC_AUTH_TOKEN` / `FLEET_EXTERNAL_TOKEN`
  retained as deprecated-but-honored fallback names during migration. No
  env var encodes `spire.mode` — the app-level contract (socket in, SVID
  out) is identical in both modes (Decision 1); mode only changes what the
  Helm chart deploys.
- **Helm values** (`spire.*`, new):
  - `spire.mode`: `own` (**default**) | `shared` — selects which block
    below applies.
  - `spire.own.*` (when `mode: own`): deploys this chart's own
    `spire-server` + `spire-agent` DaemonSet manifests, plus
    `spire.own.upstreamAuthority` (join config for the shared
    `penguintech.io` root).
  - `spire.shared.*` (when `mode: shared`, **values comments recommend
    this where skauswatch is already deployed** in the target cluster): no
    server/agent manifests — only `spire.shared.workloadSocketHostPath`
    (mounts skauswatch's existing agent socket) and
    `spire.shared.registrationRequestedFrom: skauswatch` (documents that
    entries are a cross-repo ask, not self-serve).
  - `spire.trustDomain` (default `penguintech.io`), per-service
    `spire.allowedPeers` (feeds `SPIFFEConfig.allowed_ids`) — shared by
    both modes.

## Migration sequence

1. **penguin-aaa first (Decision 2)**: contribute the Workload-API-fetch +
   mTLS/JWT-SVID wiring described in Dependencies below — the first
   dependency; nothing downstream can be built without it landing upstream
   and being released.
2. **Deploy the chosen SPIRE mode** (Decision 1): `own` — apply this
   repo's new `spire-server`/`spire-agent` Helm templates and complete the
   upstream-authority join to the root; `shared` — coordinate with
   skauswatch's team to add the three registration entries to their
   existing dal2-beta/dal2-gamma child servers (cross-repo ask). Interim:
   no code changes ship before whichever path is live to test against.
3. **Proxy gRPC server** (`grpc_server.py`): add the mTLS + JWT-SVID
   listener/interceptor, `PROXY_GRPC_AUTH_TOKEN` becomes fallback-only.
   Interim: the static token stays the *only* check, unchanged.
4. **proxy → management REST**: add outbound SVID + JWT-SVID (or OIDC
   machine JWT where no SPIRE identity exists yet) to the one real
   inter-service call surface (`/healthz` stays open as a liveness probe).
   Interim: none — net-new auth, not a credential swap.
5. **AIProxy → external fleet**: fold the existing beta/prod cert-manager
   client cert (`fleet-external-mtls.yaml:19-42`) into a proper SVID (URI
   SAN instead of plain CN); alpha/dev `FLEET_EXTERNAL_TOKEN` becomes
   fallback-only — already alpha/dev-only per the chart's comments, lowest
   urgency.
6. **Retire fallback tokens** once every environment confirms SVID issuance
   is live — remove `PROXY_GRPC_AUTH_TOKEN`/`FLEET_EXTERNAL_TOKEN` fallback
   code paths entirely, not just stop using them.

**Parallel with credential-reference-injection (Decision 3)**: that
feature's `CredentialResolver` (the proxy's own auth when it talks to
Vault/skauswatch) takes an injected auth-provider and ships first, ahead of
this migration completing, using a short-lived OIDC machine JWT — the
`security.md`-sanctioned no-SPIRE fallback, never a static token. Once step
1 above lands, this SPIFFE work swaps in an SVID-based provider behind the
same call site with no caller-visible change — a hard interface seam, not a
sequencing dependency in either direction, so both features' PRs into
`release/v0.2.X` can proceed in parallel.

## Dependencies on penguin-aaa

**Upstream contract (Decision 2 — built here first, released, then pinned
in WaddleAI; this repo ships no local reimplementation):**

```python
# penguin_aaa.authn.spiffe (fetches/rotates SVID material from the socket)
class WorkloadAPIClient:
    def __init__(self, workload_socket: str = "/run/spire/agent.sock"): ...
    async def fetch_x509_svid(self) -> X509SVIDBundle: ...
    async def fetch_jwt_svid(self, audience: str) -> JWTSVID: ...

# penguin_aaa.middleware.spiffe (Quart/ASGI: peer SVID or JWT-SVID/OIDC JWT)
class SPIFFEASGIMiddleware:
    def __init__(self, app, *, authenticator: SPIFFEAuthenticator,
                 jwt_fallback: JWTValidator): ...

# penguin_aaa.grpc.spiffe (gRPC equivalent, same check order)
class SPIFFEServerInterceptor(grpc.aio.ServerInterceptor):
    def __init__(self, authenticator: SPIFFEAuthenticator,
                 jwt_fallback: JWTValidator): ...
```

Already published: `penguin_aaa.authn.spiffe.SPIFFEAuthenticator`
(`python-aaa/src/penguin_aaa/authn/spiffe.py:26-99`, identity-comparison
only, no transport) and `validate_spiffe_id`. The three items above are
what's missing — the Workload-API-fetch/transport wiring
`SPIFFEAuthenticator`'s own docstring says doesn't exist yet.

**Stays product-local**: the `waddleai-<service>` ID list and scope-bundle
mapping; Helm wiring for `spire.mode`, the socket mount, and the internal
mTLS listener port; the token fallback-and-retire sequence.

## Test strategy

- **Unit**: generate a root → intermediate → leaf X.509 chain with
  `cryptography` (already pinned, `requirements.in:38`), each cert carrying
  a `spiffe://penguintech.io/test/...` URI SAN. Assert: (i) the leaf
  validates via the trust bundle; (ii) a *rotated* intermediate (new
  intermediate, same root) still validates a leaf issued under it; (iii)
  the verifier accepts a **new leaf under the same root** — i.e. prove
  pinning a leaf fingerprint would fail this test, since that's exactly the
  case such a check would reject; (iv) a JWT-SVID with a rotated `kid`
  validates against the refreshed JWKS, wrong `aud` is rejected. These four
  are the direct test of Decision 4's "never leaf pinning" rule.
- **Contract tests**: one shared test module asserting the middleware
  behaves identically whether identity arrives via X.509-SVID mTLS or
  JWT-SVID — run in both modes explicitly (not "primary + fallback", per
  Decision 4). Proves `spire.mode` (own/shared) and identity-mechanism
  interchangeability at the service-side contract layer.
- **E2E**: SPIRE in a kind/MicroK8s cluster (mirrors `local-alpha` context),
  **`spire.mode: own`** (Decision 4 pins e2e to the mode this repo fully
  controls) — register WaddleAI's three service IDs, deploy
  proxy+management, confirm gRPC and REST calls succeed with both
  X.509-SVID mTLS and JWT-SVID, and fail closed when the peer cert/JWT is
  absent, expired, or from the wrong trust domain.

## Decisions (2026-08-21)

1. **SPIRE host**: both topologies are supported, selected by a Helm
   values toggle `spire.mode: own | shared`, **default `own`**. `own` =
   WaddleAI runs its own child SPIRE server joined to the `penguintech.io`
   root. `shared` = WaddleAI workloads register on skauswatch's existing
   child servers. Docs/values comments recommend `shared` wherever
   skauswatch is already deployed in that cluster. The service-side
   contract is identical in both modes.
2. **Where built**: the Workload-API fetch + mTLS/JWT-SVID wiring is built
   upstream in `penguin-aaa` first (local clone
   `~/code/penguin-libs/packages/python-aaa`; `py-spiffe` already a
   declared optional extra), released, then pinned here. WaddleAI ships
   only configuration, registration entries, and call sites — first
   dependency in the migration sequence.
3. **Sequencing vs. credential-reference-injection**: parallel PRs into
   `release/v0.2.X` with a hard interface seam. That feature's
   `CredentialResolver` takes an injected auth-provider and ships first
   using a short-lived OIDC machine JWT (the `security.md`-sanctioned
   no-SPIRE fallback, never a static token); this SPIFFE work later swaps
   in an SVID-based provider with no caller changes.
4. **Identity modes + validation depth**: both X.509-SVID over mTLS and
   JWT-SVID are first-class and tested everywhere — not "mTLS preferred,
   JWT fallback." Many companies run JWT-SVID in production because mTLS
   is operationally risky; alpha uses JWT-SVID as a genuine test of that
   path, beta/gamma exercise both. Validation is at the trust root /
   intermediate CA level, never leaf pinning: X.509-SVIDs validate
   chain-to-bundle (rotating intermediates tolerated) then authorize on the
   SPIFFE ID; JWT-SVIDs validate signature against the bundle's JWKS
   (`kid` rotation tolerated) + `aud` + `exp`, then authorize on `sub`.
   Forbidden: pinning a leaf certificate fingerprint, pinning a single
   `kid`, or allowlisting by certificate serial number.
