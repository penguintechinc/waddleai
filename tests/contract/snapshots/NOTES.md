# Snapshot notes

Context a reader cannot recover from `git log` on a JSON value alone.

## `mgmt_auth_login.json` / `mgmt_auth_refresh.json` — `expires_in` 86400 -> 3600 (2026-09-21)

**This is not a contract change, and nobody shortened the session.** It is the
correction of a value that never matched reality.

`/auth/login` and `/auth/refresh` returned a hardcoded `expires_in: 86400`
while the token they returned alongside it was signed with a 3600s `exp` --
`penguin_aaa`'s `issue_token_set()` ignores `claims.exp` and derives expiry
from the provider's `token_ttl`, which has been 1h (`TOKEN_TTL_HOURS`,
default 1) the whole time. The advertised lifetime was simply wrong: clients
scheduled their refresh eleven hours after the session had already died.

The snapshot pinned the wrong number as the contract, so the golden file was
asserting a client-facing lie. Sessions are the same length today as they were
before this commit; only the number we report about them changed.

Verify rather than trust this note -- decode any issued token and compare:

```python
jwt.decode(access_token, options={"verify_signature": False})  # exp - iat == 3600
```

Fixed in the audit-2026-09-14 hardening pass alongside real 1h/24h lifetime
handling. See `services/management/app/api/v1/auth.py` (`issue_access_token`).
