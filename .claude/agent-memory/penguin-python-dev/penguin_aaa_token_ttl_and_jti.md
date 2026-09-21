---
name: penguin-aaa-token-ttl-and-jti
description: penguin_aaa issue_token_set ignores claims.exp and uses config.token_ttl; max_token_ttl defaults to 1h and raises at construction, so TOKEN_TTL_HOURS>1 crashed startup. jti is already stamped on every token.
metadata:
  type: project
---

`penguin_aaa.authn.oidc_provider.OIDCProvider.issue_token_set()` **ignores
`claims.exp` entirely** and computes `exp = now + self._config.token_ttl`.
`shared/auth/penguin_auth.py:user_context_to_claims()` sets `exp=now+24h`,
which is dead code — measured real TTL was 3600s.

Consequences found during the 2026-09-14 security audit fixes:

- The 2026-09-14 audit's "tokens last 24h" finding was **wrong**. The signed
  tokens were already 1h. The real defect was `login`/`refresh` advertising a
  hardcoded `expires_in: 86400` against a 3600s token, so clients scheduled a
  refresh 11h after the session had already died.
- `create_token(..., expires_hours: int = 24)` was a **dead parameter** — it
  was never passed to `issue_token()`.
- `OIDCProviderConfig.max_token_ttl` defaults to **1 hour** and
  `__post_init__` raises `ValueError` when `token_ttl > max_token_ttl`. So
  `TOKEN_TTL_HOURS=2` **crashed the service at startup** rather than issuing a
  2h token. Any provider built with a longer TTL must declare `max_token_ttl`
  explicitly.
- **`jti` is already present** on every issued token (`issue_token_set` mints a
  uuid4). No change to the issuing path is needed to build a revocation
  denylist. `penguin_auth.verify_token()` drops it when rebuilding `Claims`,
  so read it with a second unverified `jwt.decode` *after* the verified call.
- `penguin_aaa.token_store.redis.RedisTokenStore` already implements
  `add_revoked_jti`/`is_jti_revoked` — reuse it instead of hand-rolling.
  Depend on a local 2-method `Protocol`, not the full `TokenStore` protocol
  (which also demands refresh-token and nonce methods, and will fail
  `mypy --strict` on any partial test double).

**Why:** these three facts invert what an audit report (or the code's own
signatures) appear to say. **How to apply:** before "fixing" a token-lifetime
finding here, decode an actually-issued token and read `exp - iat`.

See [[waddleai-management-test-harness-gotchas]] for the test-side keystore trap.
