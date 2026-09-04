# Provider Destination Failover

Serve one logical model from your own destinations in priority order, each with its
own credential. Example: point `claude-sonnet-4` at your AWS Bedrock account first,
and fall back to your Anthropic Team key when Bedrock is throttled or down.

This is *same model, alternate destination* failover — distinct from the
provider-qualified `provider:model` pin described in
[OpenAI Compatible API](../api/openai-compatible.md), which restricts (never expands)
which destinations are eligible. It also composes with, and does not replace, the
cross-provider *model substitution* policy (`routing_policies.provider_failover`).

## How it works

- A **destination** is `(provider, credential, provider-specific model id, region, timeout)`.
- Destinations are ordered by `priority`: `0` = active, `≥1` = standby (tried ascending).
- Failover is **implicit** when two or more enabled destinations exist for one model —
  there is no separate policy knob to turn on.
- Only **retryable** failures fail over: timeouts, connection errors, HTTP 429
  (incl. Bedrock `ThrottlingException`), and 5xx (incl. Anthropic 529/503). A 4xx
  (a bad key, a bad request) is **surfaced to you**, never failed over.
- A per-destination circuit breaker trips after 3 consecutive failures and holds the
  destination out for 60 s (one half-open probe on recovery).
- At most **5** enabled destinations per model; each attempt is bounded by
  `timeout_seconds` (default 30). Worst-case added latency is therefore bounded by the
  sum of `timeout_seconds` across all enabled destinations.
- Failover only ever happens **before the first byte** is flushed to the client — once
  streaming has started, the destination that started it finishes it.

### Retryable vs. non-retryable failures

| Failure | Retryable? | Notes |
|---|---|---|
| Connection error / timeout | Yes | `ProviderTimeoutError` |
| HTTP 429 | Yes | `ProviderRateLimitError` — includes Bedrock `ThrottlingException` / `ModelNotReadyException` |
| HTTP 5xx | Yes | `ProviderServerError` — includes Anthropic 529 `overloaded_error` and Bedrock `ServiceUnavailableException` / `InternalServerException` |
| HTTP 4xx (400/401/403/404/413/422) | **No** | `ProviderClientError` — surfaced to the caller as-is, never trips the breaker |

A 401/403 on a BYOK key is a config problem on *your* credential, not a reason to burn
a request against the standby — it is returned to you immediately so you notice and fix
the key, rather than being silently absorbed.

## Tenant-owned (BYOK) credentials

Credentials you create here are **owned by your org** and are used **only** by your
destinations — never by the platform pool or another org. Bedrock credential material
is a JSON object `{"aws_access_key_id","aws_secret_access_key","aws_session_token"?}`;
an empty Bedrock credential uses the ambient AWS chain / IAM role. Bearer providers
(OpenAI, Anthropic, …) take the key string directly as `material`.

Credential material is Fernet-encrypted at rest and is never returned in plaintext —
every read shows a masked label only (`api_key_masked`): bearer keys as `first4****last4`,
Bedrock material as `aws_access_key_id=AKIA****EXAMPLE`.

## Walkthrough: Bedrock active, Anthropic standby

This walkthrough assumes a Bedrock provider (`provider_id: 12`) and an Anthropic
provider (`provider_id: 7`) already exist under `/api/v1/providers` — creating
providers themselves is unrelated to this feature and outside this walkthrough.

### 1. Create a BYOK credential for your Bedrock account

```bash
curl -X POST https://your-waddleai-mgmt-host:8001/api/v1/routing/destination-credentials \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{
        "provider_id": 12,
        "label": "Bedrock prod",
        "material": "{\"aws_access_key_id\":\"AKIAEXAMPLE1234\",\"aws_secret_access_key\":\"<secret>\"}"
      }'
```

```json
{
  "id": 41,
  "provider_id": 12,
  "label": "Bedrock prod",
  "api_key_masked": "aws_access_key_id=AKIA****1234",
  "owner_org_id": 3,
  "enabled": true
}
```

### 2. Create a BYOK credential for your Anthropic Team key

```bash
curl -X POST https://your-waddleai-mgmt-host:8001/api/v1/routing/destination-credentials \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{
        "provider_id": 7,
        "label": "Anthropic Team key",
        "material": "sk-ant-api03-EXAMPLEKEY0000000000000000"
      }'
```

```json
{
  "id": 42,
  "provider_id": 7,
  "label": "Anthropic Team key",
  "api_key_masked": "sk-a****0000",
  "owner_org_id": 3,
  "enabled": true
}
```

### 3. Create the active destination (priority 0, Bedrock)

```bash
curl -X POST https://your-waddleai-mgmt-host:8001/api/v1/routing/destinations \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{
        "model": "claude-sonnet-4",
        "priority": 0,
        "provider_id": 12,
        "credential_id": 41,
        "provider_model_id": "anthropic.claude-sonnet-4-v1:0",
        "region": "us-west-2"
      }'
```

### 4. Create the standby (priority 1, Anthropic direct)

```bash
curl -X POST https://your-waddleai-mgmt-host:8001/api/v1/routing/destinations \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{
        "model": "claude-sonnet-4",
        "priority": 1,
        "provider_id": 7,
        "credential_id": 42,
        "provider_model_id": "claude-sonnet-4-20250514"
      }'
```

### 5. Call `claude-sonnet-4` as usual

```bash
curl -X POST https://your-waddleai-proxy.com/v1/chat/completions \
  -H "Authorization: Bearer <your-waddleai-key>" -H "Content-Type: application/json" \
  -d '{"model": "claude-sonnet-4", "messages": [{"role": "user", "content": "Hello"}]}'
```

When Bedrock is healthy, the response is served from Bedrock and `usage.waddleai.destination`
reports one successful attempt at `priority: 0`. When Bedrock is throttled or down, the
response is served from Anthropic direct using your Team key instead — the caller sees a
normal response either way, only the `usage.waddleai.destination` marker differs:

```json
{
  "usage": {
    "prompt_tokens": 42,
    "completion_tokens": 128,
    "total_tokens": 170,
    "waddleai": {
      "destination": {
        "id": 42,
        "priority": 1,
        "role": "standby",
        "provider": "anthropic",
        "model": "claude-sonnet-4",
        "attempts": [
          {"destination_id": 41, "provider": "bedrock", "outcome": "failed", "reason": "rate_limit"},
          {"destination_id": 42, "provider": "anthropic", "outcome": "ok", "reason": null}
        ]
      }
    }
  }
}
```

No endpoint URLs, credential ids used for decryption, or credential material ever
appear in this marker — `attempts` names only destination ids, provider types, and a
stable outcome/reason label.

## Managing destinations

| Action | Request |
|---|---|
| List this org's destinations for a model | `GET /api/v1/routing/destinations?model=claude-sonnet-4` |
| Change priority, disable, or swap the credential | `PATCH /api/v1/routing/destinations/{id}` |
| Remove a destination | `DELETE /api/v1/routing/destinations/{id}` |
| List this org's BYOK credentials | `GET /api/v1/routing/destination-credentials` |
| Remove a BYOK credential | `DELETE /api/v1/routing/destination-credentials/{id}` |

Deleting a credential still referenced by a destination does not delete the destination —
that destination's `credential_id` is set to `NULL` (falls back to the provider's platform
pool / ambient credentials on its next resolve) rather than leaving a dangling reference.
Full method/path/scope table: [Management API Reference](../api/management-api.md).

## Enablement

Enterprise-tier, behind the `waddleai.provider_failover` feature flag (default OFF)
**and** the `waddleai_provider_failover` license entitlement — both are required, fail
closed on any evaluation error. When the flag is off or the entitlement is absent,
requests take the existing single-destination path unchanged: no destinations are
consulted and behavior is byte-identical to a deployment without this feature.

## Deferred to Phase 2

Not yet available — plan accordingly:

- **Web UI.** Configure destinations and BYOK credentials via the REST API above; no
  console screen exists for this yet.
- **Shared circuit breaker state.** The breaker is in-process per proxy replica today —
  each replica trips independently on its own evidence rather than sharing state across
  replicas via Valkey.
- **Periodic destination health probes.** Destinations are only marked unhealthy by
  live request failures, not by a background prober.
- **User/API-key-level destination scopes.** Destinations are org-wide; there is no
  per-user or per-key override.
