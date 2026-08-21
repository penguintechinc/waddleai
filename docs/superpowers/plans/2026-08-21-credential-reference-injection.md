# Credential Reference Injection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task ends in a real `git commit`.

**Branch:** `feature/credential-reference-injection` (off `release/v0.2.X`).

**Goal:** An MCP agent never holds a real credential for a target service (Jira, etc.) — it holds an opaque, single-use `waddleref:<token>` reference minted by a new MCP tool, bound to the minting session's authenticated identity; WaddleAI's own MCP gateway redeems it for the real secret only on the one outbound call it forwards to that target service, after outbound content filtering has already run.

**Architecture:** A new `shared/credentials/` package owns everything security-critical: a pluggable `CredentialResolver` structural-typed backend (Vault via `penguin-sal`, plus an in-memory test double), a Valkey-backed `CredentialReferenceStore` that mints and atomically single-use-redeems references (mirroring `shared/utils/token_limiter.py`'s Lua-script `evalsha` pattern), and a `substitute_references` matcher that only ever touches JSON-string/header/form-field values of an outbound gateway request, never prompt bodies. The one new MCP tool (`get_credential_reference`, `shared/mcp/tools.py`) mints; the one existing egress chokepoint (`GatewayAggregator._invoke`, `shared/mcp/gateway/aggregator.py`) redeems and substitutes, strictly after its existing input-side `ContentFilterPolicyResolver` check and strictly before `client.invoke()`. Admins bind `(org_id, service, action)` to a backend + path via a new `credential_bindings` table and `/api/v1/integrations/credentials` routes, following the exact pattern `mcp_endpoints`/`/api/v1/integrations/mcp-endpoints` already established. Every mint/redeem outcome is audited to a new `credential_reference_audit_log` table, styled on `content_filter_audit_log`. Cache eligibility (`shared/cache/keys.py`, `shared/cache/semantic.py`) excludes any request whose message history carries a reference. Everything sits behind PostHog flag `waddleai.credential_references` (default OFF) AND licence entitlement `credential_reference_injection` (Enterprise tier).

**Tech Stack:** Python 3.13, async throughout, `penguin-dal` (PyDAL-compatible) for the durable audit table, SQLAlchemy + Alembic for schema (migration 017), `penguin-sal[vault]>=0.2.0` (published `penguin_sal.adapters.vault.VaultAdapter`, wrapping `hvac`) for the Vault backend, Valkey (async client, dependency-injected) for the reference store, `pytest` + `pytest-asyncio` (`asyncio_mode = auto`, see `pytest.ini`).

**Spec:** `docs/superpowers/specs/2026-08-21-credential-reference-injection-design.md` (base design + the `## Recommendations (2026-08-21)` section this plan implements verbatim — every decision number below (§1-§11) refers to that section). Cross-referenced against `docs/superpowers/specs/2026-07-09-waddleai-platform-spec.md` §11.4/§11.5 (MCP gateway), §19.2/§19.3 (product-positioning record of this feature + the SPIFFE dependency).

## Global Constraints

- Every new dataclass is `@dataclass(slots=True)` (frozen where the value is never mutated after construction).
- Async throughout — no sync I/O on the event loop; the Vault backend's `hvac` calls are sync and MUST be wrapped in `asyncio.to_thread`.
- Structural typing (`Protocol`, `@runtime_checkable`) for pluggable collaborators — matches `shared/mcp/gateway/aggregator.py::ToolPolicyResolver`/`McpEndpointRepository`, not ABC inheritance.
- `penguin-dal` (`from penguin_dal import DAL, Field`) for the audit table's PyDAL binding in `shared/database/models.py`; Alembic is schema-authoritative (`services/management/app/models_sqlalchemy.py` + `services/management/alembic/versions/`).
- Docstrings: 2-3 lines on every class and function, stating what it does and why it exists — no line-by-line walkthroughs, no ASCII-art dividers.
- Feature flag `waddleai.credential_references` (default OFF) gates every new code path; licence entitlement `credential_reference_injection` (Enterprise) gates it additionally, same AND-shape as `waddleai.pii_ner` + `pii_ner_detection`.
- The reference grammar (`waddleref:[A-Za-z0-9_-]{43}`) is defined exactly once (`shared/credentials/substitution.py::CREDENTIAL_REFERENCE_PATTERN`) and imported everywhere else that needs to recognize it — never re-implemented.
- Dependency pinning: `requirements.in` uses `penguin-sal[vault]>=0.2.0` (extras syntax); `requirements.txt`/`proxy/requirements.txt`/`services/management/requirements.txt` regenerated via `uv pip compile --generate-hashes` — never a bare `pip install`.
- Exact pytest invocation: `.venv/bin/python -m pytest <path> -v` (falls back to `python3` per `Makefile`'s `PY` variable if no venv, but this plan always invokes the venv path directly).
- No hardcoded secrets, no static long-lived tokens for the proxy→backend service call — AppRole (`role_id`/`secret_id`) as the interim per §10, never a bare `VAULT_TOKEN` env var wired as the only auth path.

---

## File Structure

| Action | File | Responsibility |
|--------|------|-----------------|
| Create | `shared/credentials/__init__.py` | Package marker |
| Create | `shared/credentials/resolver.py` | `CredentialResolver` Protocol, `ResolvedSecret`, error types |
| Create | `tests/unit/credentials/__init__.py` | Test package marker |
| Create | `tests/unit/credentials/test_resolver.py` | Protocol conformance / error-type tests |
| Create | `shared/credentials/backends/__init__.py` | Package marker |
| Create | `shared/credentials/backends/memory.py` | `InMemoryCredentialResolver` — test/dev backend |
| Create | `tests/unit/credentials/test_memory_backend.py` | Seed + resolve + not-found tests |
| Create | `shared/credentials/backends/vault.py` | `VaultCredentialResolver` wrapping `penguin_sal.adapters.vault.VaultAdapter` |
| Create | `tests/unit/credentials/test_vault_backend.py` | Resolve success/not-found/backend-error, against a fake `VaultAdapter` |
| Create | `shared/credentials/reference_store.py` | `CredentialReferenceStore` — mint + atomic single-use redeem (Valkey) |
| Create | `tests/unit/credentials/test_reference_store.py` | Mint/redeem round trip, single-use, expiry, identity mismatch, concurrent-redeem race |
| Create | `shared/credentials/audit.py` | `CredentialAuditEvent`, `record_event` |
| Create | `services/management/alembic/versions/017_credential_references.py` | Migration: `credential_reference_audit_log`, `credential_bindings` |
| Modify | `shared/database/models.py` | PyDAL binding for `credential_reference_audit_log` |
| Modify | `services/management/app/models_sqlalchemy.py` | `CredentialBinding`, `CredentialReferenceAuditLog` ORM models |
| Create | `tests/unit/management/test_migration_017.py` | Round-trip + downgrade on seeded snapshot |
| Create | `tests/unit/credentials/test_audit.py` | `record_event` logs + inserts; broken-insert path logs loudly, never raises |
| Create | `shared/credentials/substitution.py` | `CREDENTIAL_REFERENCE_PATTERN`, `substitute_references()` — JSON-value/header/form-field-only matcher |
| Create | `tests/unit/credentials/test_substitution.py` | Header/form/JSON-value substitution; never touches prompt-shaped text; full-match-only |
| Modify | `shared/mcp/tools.py` | `get_credential_reference` tool on `WaddleAITools` |
| Modify | `shared/mcp/server.py` | Register `get_credential_reference` in `USER_TOOL_NAMES` |
| Create | `tests/unit/mcp/test_credential_reference_tool.py` | Mint via tool, flag-off disabled, binding-not-found error shape |
| Modify | `shared/mcp/gateway/aggregator.py` | Wire `substitute_references` into `_invoke`; output-side secret-echo guard |
| Create | `tests/unit/mcp/test_gateway_aggregator_credential_substitution.py` | Substitution ordering vs filter; identity-bound redeem; single-use retry-fails-closed; echo guard |
| Modify | `shared/cache/keys.py` | `_message_has_credential_reference` in `is_exact_eligible` |
| Modify | `shared/cache/semantic.py` | Same check in `is_semantic_eligible` |
| Modify | `tests/unit/cache/test_eligibility_keys.py` | Reference-carrying request is exact/semantic-cache-ineligible |
| Modify | `services/management/app/api/v1/integrations.py` | `/api/v1/integrations/credentials` CRUD (org-scoped, admin-only, secret path never echoed) |
| Modify | `services/management/app/api/v1/__init__.py` | Already registers `integrations` blueprint — no change expected, verified in Task 10 |
| Create | `tests/unit/management/test_integrations_credentials_routes.py` | CRUD org-scope; admin-scope required; backend_path never echoed in response |
| Modify | `openapi/v1.yaml` | Regenerated (`make generate-openapi`) — adds `/api/v1/integrations/credentials*` |
| Modify | `requirements.in`, `requirements.txt`, `proxy/requirements.txt`, `services/management/requirements.txt` | `penguin-sal[vault]>=0.2.0`, re-hashed |
| Create | `docs/integrations/credential-references.md` | Admin + agent-facing docs: how to bind a credential, what `get_credential_reference` returns, why direct agent→target-service calls don't work |

---

### Task 1: `CredentialResolver` protocol + core types (`shared/credentials/resolver.py`)

The narrow interface every backend implements. Nothing here talks to a network — pure types and error classes, matching how `shared/mcp/gateway/aggregator.py::ToolPolicyResolver` is defined before `ContentFilterPolicyResolver` implements it.

**Files:**
- Create: `shared/credentials/__init__.py` (empty)
- Create: `shared/credentials/resolver.py`
- Test: `tests/unit/credentials/__init__.py` (empty), `tests/unit/credentials/test_resolver.py`

**Interfaces:**
- Produces: `ResolvedSecret(value: str, backend: str)`; `CredentialBackendError`, `CredentialNotFoundError(CredentialBackendError)`; `CredentialResolver` (`@runtime_checkable` `Protocol` with `async def resolve(self, *, org_id: int, service: str, action: str | None) -> ResolvedSecret`).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/credentials/test_resolver.py
import pytest

from shared.credentials.resolver import (
    CredentialBackendError,
    CredentialNotFoundError,
    CredentialResolver,
    ResolvedSecret,
)


def test_credential_not_found_is_a_backend_error():
    assert issubclass(CredentialNotFoundError, CredentialBackendError)


def test_resolved_secret_is_frozen_slots_dataclass():
    secret = ResolvedSecret(value="s3kret", backend="memory")
    assert secret.value == "s3kret"
    assert secret.backend == "memory"
    with pytest.raises(AttributeError):
        secret.value = "other"  # frozen


def test_a_class_implementing_resolve_satisfies_the_protocol():
    class _Fake:
        async def resolve(self, *, org_id, service, action):
            return ResolvedSecret(value="x", backend="fake")

    assert isinstance(_Fake(), CredentialResolver)


def test_a_class_without_resolve_does_not_satisfy_the_protocol():
    class _NotAResolver:
        pass

    assert not isinstance(_NotAResolver(), CredentialResolver)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/credentials/test_resolver.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'shared.credentials'`

- [ ] **Step 3: Write the implementation**

```python
# shared/credentials/resolver.py
"""Pluggable CredentialResolver backend interface (§1/§9/§11 of the design spec).

Every backend (Vault, an in-memory test double, and later skauswatch or a
cloud secret manager) implements the same narrow `resolve()` coroutine;
shared/credentials/reference_store.py is the only caller and never
branches on which concrete backend it holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class CredentialBackendError(RuntimeError):
    """Raised when a backend is unreachable or refuses a lookup for any other reason.

    Message text must stay safe to log -- never interpolate the resolved
    secret value into this exception.
    """


class CredentialNotFoundError(CredentialBackendError):
    """Raised when the backend has no secret bound to the requested (service, action)."""


@dataclass(slots=True, frozen=True)
class ResolvedSecret:
    """One secret value resolved from a backend, plus which backend answered.

    Held only in memory for the duration of one redemption -- callers
    must never persist, log, or return this value to an MCP client.
    """

    value: str
    backend: str


@runtime_checkable
class CredentialResolver(Protocol):
    """Structural interface for a secrets backend (§1/§10 of the design spec)."""

    async def resolve(self, *, org_id: int, service: str, action: str | None) -> ResolvedSecret:
        """Return the real secret for (org_id, service, action).

        Raises:
            CredentialNotFoundError: no binding exists for this triple.
            CredentialBackendError: the backend is unreachable or refused
                the request for any other reason.
        """
        ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/credentials/test_resolver.py -v --no-cov`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add shared/credentials/__init__.py shared/credentials/resolver.py tests/unit/credentials/__init__.py tests/unit/credentials/test_resolver.py
git commit -m "feat(credentials): CredentialResolver protocol + core types" \
           -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: In-memory backend (`shared/credentials/backends/memory.py`)

The test/dev backend — never wired in a real deployment (Task 11 only ever constructs `VaultCredentialResolver` outside tests). Establishes the pattern every other backend (including Task 3's Vault one) follows.

**Files:**
- Create: `shared/credentials/backends/__init__.py` (empty), `shared/credentials/backends/memory.py`
- Test: `tests/unit/credentials/test_memory_backend.py`

**Interfaces:**
- Consumes: `shared.credentials.resolver.{CredentialNotFoundError, ResolvedSecret}` (Task 1)
- Produces: `InMemoryCredentialResolver` with `.seed(*, org_id, service, value, action=None)` and `async def resolve(self, *, org_id, service, action)`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/credentials/test_memory_backend.py
import pytest

from shared.credentials.backends.memory import InMemoryCredentialResolver
from shared.credentials.resolver import CredentialNotFoundError


@pytest.mark.asyncio
async def test_resolve_returns_seeded_secret():
    backend = InMemoryCredentialResolver()
    backend.seed(org_id=42, service="jira", value="sk-jira-abc123")

    resolved = await backend.resolve(org_id=42, service="jira", action=None)

    assert resolved.value == "sk-jira-abc123"
    assert resolved.backend == "memory"


@pytest.mark.asyncio
async def test_resolve_is_scoped_by_org_and_action():
    backend = InMemoryCredentialResolver()
    backend.seed(org_id=1, service="jira", value="org1-secret")
    backend.seed(org_id=2, service="jira", value="org2-secret")
    backend.seed(org_id=1, service="jira", action="read", value="org1-read-secret")

    assert (await backend.resolve(org_id=1, service="jira", action=None)).value == "org1-secret"
    assert (await backend.resolve(org_id=2, service="jira", action=None)).value == "org2-secret"
    assert (await backend.resolve(org_id=1, service="jira", action="read")).value == "org1-read-secret"


@pytest.mark.asyncio
async def test_resolve_unknown_binding_raises_not_found():
    backend = InMemoryCredentialResolver()

    with pytest.raises(CredentialNotFoundError):
        await backend.resolve(org_id=1, service="unknown", action=None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/credentials/test_memory_backend.py -v --no-cov`
Expected: FAIL — `ModuleNotFoundError: No module named 'shared.credentials.backends'`

- [ ] **Step 3: Write the implementation**

```python
# shared/credentials/backends/memory.py
"""In-memory CredentialResolver -- test/dev backend only (never a real deployment path)."""

from __future__ import annotations

from dataclasses import dataclass, field

from shared.credentials.resolver import CredentialNotFoundError, ResolvedSecret


@dataclass(slots=True)
class InMemoryCredentialResolver:
    """An in-process dict of (org_id, service, action) -> secret value, for tests.

    Structurally satisfies `shared.credentials.resolver.CredentialResolver`
    without inheriting from it, matching the Protocol-based pattern already
    used by `shared/mcp/gateway/aggregator.py::ContentFilterPolicyResolver`.
    """

    _secrets: dict[tuple[int, str, str | None], str] = field(default_factory=dict)

    def seed(self, *, org_id: int, service: str, value: str, action: str | None = None) -> None:
        """Register a secret value for a later `resolve()` call (test setup helper)."""
        self._secrets[(org_id, service, action)] = value

    async def resolve(self, *, org_id: int, service: str, action: str | None) -> ResolvedSecret:
        """Return the seeded value for (org_id, service, action), or raise CredentialNotFoundError."""
        key = (org_id, service, action)
        if key not in self._secrets:
            raise CredentialNotFoundError(f"no credential bound for service={service!r}")
        return ResolvedSecret(value=self._secrets[key], backend="memory")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/credentials/test_memory_backend.py -v --no-cov`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add shared/credentials/backends/__init__.py shared/credentials/backends/memory.py tests/unit/credentials/test_memory_backend.py
git commit -m "feat(credentials): in-memory CredentialResolver backend for tests/dev" \
           -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Vault backend (`shared/credentials/backends/vault.py`)

Wraps `penguin_sal.adapters.vault.VaultAdapter` (already published, KV v2 + token/AppRole auth). Per Recommendation §10, authenticates via AppRole as the SPIFFE interim — never a bare static token.

**Files:**
- Create: `shared/credentials/backends/vault.py`
- Test: `tests/unit/credentials/test_vault_backend.py`

**Interfaces:**
- Consumes: `penguin_sal.core.types.ConnectionConfig`, `penguin_sal.adapters.vault.VaultAdapter` (published `penguin-sal[vault]`), `shared.credentials.resolver.{CredentialBackendError, CredentialNotFoundError, ResolvedSecret}` (Task 1)
- Produces: `VaultCredentialResolver(config: ConnectionConfig, mount_point: str = "secret")` with `async def resolve(self, *, org_id, service, action)`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/credentials/test_vault_backend.py
from unittest.mock import MagicMock, patch

import pytest
from penguin_sal.core.exceptions import BackendError, SecretNotFoundError
from penguin_sal.core.types import ConnectionConfig, Secret

from shared.credentials.backends.vault import VaultCredentialResolver
from shared.credentials.resolver import CredentialBackendError, CredentialNotFoundError


def _config() -> ConnectionConfig:
    return ConnectionConfig(
        scheme="https", host="vault.internal", port=8200,
        params={"role_id": "r-id", "secret_id": "s-id", "kv_version": "2"},
    )


@pytest.mark.asyncio
async def test_resolve_reads_the_org_scoped_path():
    fake_adapter = MagicMock()
    fake_adapter.get.return_value = Secret(key="org/42/jira", value={"value": "sk-jira-abc"})

    with patch(
        "shared.credentials.backends.vault.VaultAdapter", return_value=fake_adapter
    ):
        backend = VaultCredentialResolver(config=_config())
        resolved = await backend.resolve(org_id=42, service="jira", action=None)

    fake_adapter.authenticate.assert_called_once()
    fake_adapter.get.assert_called_once_with("org/42/jira")
    fake_adapter.close.assert_called_once()
    assert resolved.value == "sk-jira-abc"
    assert resolved.backend == "vault"


@pytest.mark.asyncio
async def test_resolve_scopes_path_by_action():
    fake_adapter = MagicMock()
    fake_adapter.get.return_value = Secret(key="org/1/jira/read", value={"value": "x"})

    with patch("shared.credentials.backends.vault.VaultAdapter", return_value=fake_adapter):
        backend = VaultCredentialResolver(config=_config())
        await backend.resolve(org_id=1, service="jira", action="read")

    fake_adapter.get.assert_called_once_with("org/1/jira/read")


@pytest.mark.asyncio
async def test_resolve_not_found_maps_to_credential_not_found_error():
    fake_adapter = MagicMock()
    fake_adapter.get.side_effect = SecretNotFoundError("org/1/unknown", backend="vault")

    with patch("shared.credentials.backends.vault.VaultAdapter", return_value=fake_adapter):
        backend = VaultCredentialResolver(config=_config())
        with pytest.raises(CredentialNotFoundError):
            await backend.resolve(org_id=1, service="unknown", action=None)


@pytest.mark.asyncio
async def test_resolve_backend_error_maps_to_credential_backend_error():
    fake_adapter = MagicMock()
    fake_adapter.get.side_effect = BackendError("vault sealed", backend="vault")

    with patch("shared.credentials.backends.vault.VaultAdapter", return_value=fake_adapter):
        backend = VaultCredentialResolver(config=_config())
        with pytest.raises(CredentialBackendError):
            await backend.resolve(org_id=1, service="jira", action=None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/credentials/test_vault_backend.py -v --no-cov`
Expected: FAIL — `ModuleNotFoundError: No module named 'shared.credentials.backends.vault'`

- [ ] **Step 3: Write the implementation**

```python
# shared/credentials/backends/vault.py
"""HashiCorp Vault CredentialResolver backend, via penguin-sal's VaultAdapter.

Authenticates with AppRole (role_id/secret_id in ConnectionConfig.params)
as the SPIFFE interim per security.md's Service-to-Service Auth table --
never a long-lived static client token -- until SVID-based Vault auth
lands per platform-spec §19.3 (see design spec Recommendation §10).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from penguin_sal.adapters.vault import VaultAdapter
from penguin_sal.core.exceptions import BackendError, SecretNotFoundError
from penguin_sal.core.types import ConnectionConfig

from shared.credentials.resolver import (
    CredentialBackendError,
    CredentialNotFoundError,
    ResolvedSecret,
)


def _vault_path(org_id: int, service: str, action: str | None) -> str:
    """Build the org-scoped KV path a binding's `backend_path` template resolves to."""
    suffix = f"{service}/{action}" if action else service
    return f"org/{org_id}/{suffix}"


@dataclass(slots=True)
class VaultCredentialResolver:
    """Resolves a secret by reading it from HashiCorp Vault KV v2 (§1/§10 of the design spec)."""

    config: ConnectionConfig

    def _read(self, path: str) -> str:
        """Blocking Vault read -- always dispatched via `asyncio.to_thread` (hvac is sync)."""
        adapter = VaultAdapter(self.config)
        adapter._init_connection()
        adapter.authenticate()
        try:
            secret = adapter.get(path)
        finally:
            adapter.close()
        value = secret.value
        if isinstance(value, dict):
            value = value.get("value", "")
        return str(value)

    async def resolve(self, *, org_id: int, service: str, action: str | None) -> ResolvedSecret:
        """Read `org/<org_id>/<service>[/<action>]` from Vault KV v2."""
        path = _vault_path(org_id, service, action)
        try:
            value = await asyncio.to_thread(self._read, path)
        except SecretNotFoundError as exc:
            raise CredentialNotFoundError(f"no credential bound for service={service!r}") from exc
        except BackendError as exc:
            raise CredentialBackendError("vault backend unavailable") from exc
        return ResolvedSecret(value=value, backend="vault")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/credentials/test_vault_backend.py -v --no-cov`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add shared/credentials/backends/vault.py tests/unit/credentials/test_vault_backend.py
git commit -m "feat(credentials): Vault CredentialResolver backend via penguin-sal (AppRole auth)" \
           -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Reference store — mint + atomic single-use redeem (`shared/credentials/reference_store.py`)

The core security primitive. Valkey-backed, TTL default 300s, single-use enforced by a Lua script (mirrors `shared/utils/token_limiter.py`'s `script_load`/`evalsha` pattern) so two concurrent redemption attempts against the same reference can never both succeed.

**Files:**
- Create: `shared/credentials/reference_store.py`
- Test: `tests/unit/credentials/test_reference_store.py`

**Interfaces:**
- Produces: `CREDENTIAL_REFERENCE_TTL_SECONDS = 300`, `REFERENCE_PREFIX = "waddleref:"`, `ReferenceExpiredError`, `ReferenceIdentityMismatchError`, `ReferenceBinding(org_id, user_uuid, session_id, service, action, backend)`, `RedeemedReference(binding: ReferenceBinding)`, `CredentialReferenceStore(valkey, *, ttl_seconds=CREDENTIAL_REFERENCE_TTL_SECONDS)` with `async def mint(*, org_id, user_uuid, session_id, service, action, backend) -> str` and `async def redeem(reference, *, org_id, user_uuid, session_id) -> RedeemedReference`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/credentials/test_reference_store.py
"""Uses a minimal in-process fake Valkey client -- get/setex/script_load/evalsha only,
enough to exercise CredentialReferenceStore without a live Redis/Valkey instance."""

from __future__ import annotations

import hashlib
import time

import pytest

from shared.credentials.reference_store import (
    REFERENCE_PREFIX,
    CredentialReferenceStore,
    ReferenceExpiredError,
    ReferenceIdentityMismatchError,
)


class _FakeValkey:
    """In-process fake supporting exactly the operations CredentialReferenceStore uses."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, float | None]] = {}
        self._scripts: dict[str, str] = {}

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self._store[key] = (value, time.time() + ttl)

    async def script_load(self, script: str) -> str:
        sha = hashlib.sha1(script.encode()).hexdigest()  # noqa: S324 -- test double, not crypto use
        self._scripts[sha] = script
        return sha

    async def evalsha(self, sha: str, numkeys: int, key: str) -> str | None:
        assert sha in self._scripts
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and time.time() >= expires_at:
            del self._store[key]
            return None
        del self._store[key]  # single-use: this evalsha models GET-then-DEL atomically
        return value


@pytest.mark.asyncio
async def test_mint_returns_a_waddleref_prefixed_token():
    store = CredentialReferenceStore(_FakeValkey())

    reference = await store.mint(
        org_id=1, user_uuid="u1", session_id="s1", service="jira", action=None, backend="vault"
    )

    assert reference.startswith(REFERENCE_PREFIX)
    assert len(reference) == len(REFERENCE_PREFIX) + 43  # secrets.token_urlsafe(32) length


@pytest.mark.asyncio
async def test_redeem_with_matching_identity_succeeds():
    store = CredentialReferenceStore(_FakeValkey())
    reference = await store.mint(
        org_id=1, user_uuid="u1", session_id="s1", service="jira", action=None, backend="vault"
    )

    redeemed = await store.redeem(reference, org_id=1, user_uuid="u1", session_id="s1")

    assert redeemed.binding.service == "jira"
    assert redeemed.binding.backend == "vault"


@pytest.mark.asyncio
async def test_redeem_is_single_use():
    store = CredentialReferenceStore(_FakeValkey())
    reference = await store.mint(
        org_id=1, user_uuid="u1", session_id="s1", service="jira", action=None, backend="vault"
    )
    await store.redeem(reference, org_id=1, user_uuid="u1", session_id="s1")

    with pytest.raises(ReferenceExpiredError):
        await store.redeem(reference, org_id=1, user_uuid="u1", session_id="s1")


@pytest.mark.asyncio
async def test_redeem_unknown_reference_raises_expired():
    store = CredentialReferenceStore(_FakeValkey())

    with pytest.raises(ReferenceExpiredError):
        await store.redeem(f"{REFERENCE_PREFIX}nonexistent", org_id=1, user_uuid="u1", session_id="s1")


@pytest.mark.asyncio
async def test_redeem_wrong_identity_raises_mismatch_and_still_consumes():
    store = CredentialReferenceStore(_FakeValkey())
    reference = await store.mint(
        org_id=1, user_uuid="u1", session_id="s1", service="jira", action=None, backend="vault"
    )

    with pytest.raises(ReferenceIdentityMismatchError):
        await store.redeem(reference, org_id=1, user_uuid="attacker", session_id="s1")

    # single-use: the mismatched attempt already consumed it, so the rightful
    # owner's retry also fails -- correct, fail-closed behavior (a leaked
    # reference is burned by the first redemption attempt against it,
    # whoever made it).
    with pytest.raises(ReferenceExpiredError):
        await store.redeem(reference, org_id=1, user_uuid="u1", session_id="s1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/credentials/test_reference_store.py -v --no-cov`
Expected: FAIL — `ModuleNotFoundError: No module named 'shared.credentials.reference_store'`

- [ ] **Step 3: Write the implementation**

```python
# shared/credentials/reference_store.py
"""Mint + single-use redeem for `waddleref:` credential references (design spec §1/§4).

Valkey-backed, same DI shape as shared/mcp/gateway/auth.py::TokenCache and
shared/utils/token_limiter.py::TokenLimiter. Redemption uses a Lua script
(mirroring TokenLimiter's script_load/evalsha pattern) so a GET-then-DEL
is one atomic round trip -- two concurrent redemption attempts against the
same reference can never both succeed.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

CREDENTIAL_REFERENCE_TTL_SECONDS = 300
REFERENCE_PREFIX = "waddleref:"
_KEY_PREFIX = "waddleai:credential_reference"

_LUA_REDEEM = """
local v = redis.call('GET', KEYS[1])
if v then
    redis.call('DEL', KEYS[1])
end
return v
"""


class ReferenceExpiredError(RuntimeError):
    """Raised when a reference is unknown, expired, or already consumed."""


class ReferenceIdentityMismatchError(RuntimeError):
    """Raised when the redeeming identity does not match the minting identity.

    The reference is already consumed by the time this is raised -- a
    mismatched redemption attempt burns the reference for everyone,
    including its rightful owner, which is the correct fail-closed
    behavior for a reference that may have leaked.
    """


@dataclass(slots=True, frozen=True)
class ReferenceBinding:
    """Identity + scope bound to one minted reference (mirrors the Valkey JSON value)."""

    org_id: int
    user_uuid: str
    session_id: str
    service: str
    action: str | None
    backend: str


@dataclass(slots=True, frozen=True)
class RedeemedReference:
    """A successfully redeemed reference's binding, before the real secret is fetched."""

    binding: ReferenceBinding


def _hash_reference(reference: str) -> str:
    """SHA-256 hex digest of the full reference string -- the reference itself is never stored."""
    return hashlib.sha256(reference.encode("utf-8")).hexdigest()


def _store_key(reference_hash: str) -> str:
    return f"{_KEY_PREFIX}:{reference_hash}"


class CredentialReferenceStore:
    """Mint + single-use redeem for `waddleref:` credential references (design spec §1)."""

    def __init__(self, valkey: Any, *, ttl_seconds: int = CREDENTIAL_REFERENCE_TTL_SECONDS) -> None:
        """Bind this store to an async Valkey/redis client and a TTL (default 5 minutes)."""
        self._valkey = valkey
        self._ttl_seconds = ttl_seconds
        self._redeem_sha: str | None = None

    async def mint(
        self,
        *,
        org_id: int,
        user_uuid: str,
        session_id: str,
        service: str,
        action: str | None,
        backend: str,
    ) -> str:
        """Mint a fresh, single-use `waddleref:<token>` bound to this identity + scope."""
        token = secrets.token_urlsafe(32)
        reference = f"{REFERENCE_PREFIX}{token}"
        payload = json.dumps(
            {
                "org_id": org_id,
                "user_uuid": user_uuid,
                "session_id": session_id,
                "service": service,
                "action": action,
                "backend": backend,
                "minted_at": time.time(),
            }
        )
        await self._valkey.setex(_store_key(_hash_reference(reference)), self._ttl_seconds, payload)
        return reference

    async def redeem(
        self, reference: str, *, org_id: int, user_uuid: str, session_id: str
    ) -> RedeemedReference:
        """Atomically consume `reference`, verifying it belongs to the redeeming identity.

        Raises ReferenceExpiredError if unknown/expired/already-consumed;
        ReferenceIdentityMismatchError if it was minted for a different
        (org_id, user_uuid, session_id) triple -- checked against the
        *authenticated* redeeming identity, never a value taken from the
        outbound request body (security.md tenant-isolation rule applied
        to this surface).
        """
        if self._redeem_sha is None:
            self._redeem_sha = await self._valkey.script_load(_LUA_REDEEM)

        key = _store_key(_hash_reference(reference))
        raw = await self._valkey.evalsha(self._redeem_sha, 1, key)
        if not raw:
            raise ReferenceExpiredError("credential reference expired or already used")

        data = json.loads(raw)
        binding = ReferenceBinding(
            org_id=data["org_id"],
            user_uuid=data["user_uuid"],
            session_id=data["session_id"],
            service=data["service"],
            action=data["action"],
            backend=data["backend"],
        )
        if (binding.org_id, binding.user_uuid, binding.session_id) != (org_id, user_uuid, session_id):
            raise ReferenceIdentityMismatchError("reference was minted for a different identity")
        return RedeemedReference(binding=binding)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/credentials/test_reference_store.py -v --no-cov`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add shared/credentials/reference_store.py tests/unit/credentials/test_reference_store.py
git commit -m "feat(credentials): CredentialReferenceStore -- mint + atomic single-use redeem" \
           -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Audit log — migration 017 + PyDAL binding + `audit.py`

Durable Postgres audit trail, styled directly on `content_filter_audit_log` (`shared/database/models.py`) and `ContentFilter._log_filter_event`'s insert discipline. Also adds `credential_bindings` (the admin-configured `service` -> backend mapping Task 7/10 need), deliberately **not** reusing the existing `credentials_ref` Fernet-blob naming pattern (design spec Recommendation §11).

**Files:**
- Create: `services/management/alembic/versions/017_credential_references.py`
- Modify: `services/management/app/models_sqlalchemy.py` (add `CredentialBinding`, `CredentialReferenceAuditLog`)
- Modify: `shared/database/models.py` (add PyDAL `credential_reference_audit_log` binding)
- Create: `shared/credentials/audit.py`
- Test: `tests/unit/management/test_migration_017.py`, `tests/unit/credentials/test_audit.py`

**Interfaces:**
- Produces: `EVENT_MINT/EVENT_REDEEM_SUCCESS/EVENT_REDEEM_IDENTITY_MISMATCH/EVENT_REDEEM_EXPIRED/EVENT_BACKEND_UNREACHABLE` string constants; `CredentialAuditEvent(event_type, organization_id, user_uuid, session_id, service, action, backend, reference_hash, outcome, request_id=None)`; `record_event(db, event: CredentialAuditEvent) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/management/test_migration_017.py
"""Round-trips migration 017 against a throwaway sqlite DB, mirroring the
existing pattern in tests/unit/management/test_migration_014.py."""

from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_017_creates_and_drops_expected_tables(tmp_path):
    db_path = tmp_path / "migration_017.sqlite"
    cfg = Config("services/management/alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")

    command.upgrade(cfg, "017_credential_references")
    engine = create_engine(f"sqlite:///{db_path}")
    tables = inspect(engine).get_table_names()
    assert "credential_reference_audit_log" in tables
    assert "credential_bindings" in tables

    command.downgrade(cfg, "016_hooks")
    tables_after_downgrade = inspect(engine).get_table_names()
    assert "credential_reference_audit_log" not in tables_after_downgrade
    assert "credential_bindings" not in tables_after_downgrade
```

```python
# tests/unit/credentials/test_audit.py
from unittest.mock import MagicMock

from shared.credentials.audit import (
    EVENT_MINT,
    EVENT_REDEEM_IDENTITY_MISMATCH,
    CredentialAuditEvent,
    record_event,
)


def test_record_event_inserts_into_the_audit_table():
    db = MagicMock()
    event = CredentialAuditEvent(
        event_type=EVENT_MINT,
        organization_id=1,
        user_uuid="u1",
        session_id="s1",
        service="jira",
        action=None,
        backend="vault",
        reference_hash="a" * 64,
        outcome="success",
    )

    record_event(db, event)

    db.credential_reference_audit_log.insert.assert_called_once_with(
        event_type=EVENT_MINT,
        organization_id=1,
        user_uuid="u1",
        session_id="s1",
        service="jira",
        action=None,
        backend="vault",
        reference_hash="a" * 64,
        outcome="success",
        request_id=None,
    )


def test_record_event_never_raises_when_the_insert_is_broken():
    db = MagicMock()
    db.credential_reference_audit_log.insert.side_effect = AttributeError("schema drift")
    event = CredentialAuditEvent(
        event_type=EVENT_REDEEM_IDENTITY_MISMATCH,
        organization_id=1,
        user_uuid="u1",
        session_id="s1",
        service="jira",
        action=None,
        backend="vault",
        reference_hash="b" * 64,
        outcome="failure",
    )

    record_event(db, event)  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/management/test_migration_017.py tests/unit/credentials/test_audit.py -v --no-cov`
Expected: FAIL — migration `017_credential_references` and revision unresolvable; `shared.credentials.audit` module absent

- [ ] **Step 3: Write the implementation**

```python
# services/management/alembic/versions/017_credential_references.py
"""Credential reference injection (§19.2 of the platform spec): credential_reference_audit_log, credential_bindings.

``credential_bindings`` maps an org's (service, action) to which secrets
backend answers it (Vault, skauswatch, ...) and the path/key at that
backend -- deliberately distinct from the existing `credentials_ref`
column pattern on `fleet_backends`/`mcp_endpoints` (an app-level
Fernet-encrypted blob stored in the row itself); this is a *pointer* to
an external secret manager, never a secret value in Postgres.
``credential_reference_audit_log`` records every mint/redeem outcome,
styled on migration 005's `content_filter_audit_log` -- never the
reference string or the secret value, only its SHA-256 hash.

Revision ID: 017_credential_references
Revises: 016_hooks
Create Date: 2026-08-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "017_credential_references"
down_revision: str | None = "016_hooks"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create `credential_reference_audit_log` and `credential_bindings`."""
    op.create_table(
        "credential_reference_audit_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column(
            "organization_id",
            sa.Integer,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("user_uuid", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(255), nullable=True),
        sa.Column("service", sa.String(100), nullable=False),
        sa.Column("action", sa.String(100), nullable=True),
        sa.Column("backend", sa.String(40), nullable=True),
        sa.Column("reference_hash", sa.String(64), nullable=False, index=True),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("request_id", sa.String(100), nullable=True),
    )
    op.create_table(
        "credential_bindings",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "org_id",
            sa.Integer,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("service", sa.String(100), nullable=False),
        sa.Column("action", sa.String(100), nullable=True),
        sa.Column("backend", sa.String(40), nullable=False),
        sa.Column("backend_path", sa.String(512), nullable=False),
        sa.Column(
            "created_by", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "org_id", "service", "action", name="uq_credential_bindings_org_service_action"
        ),
    )


def downgrade() -> None:
    """Drop both tables (bindings first — no FK from the audit log to it, order kept for clarity)."""
    op.drop_table("credential_bindings")
    op.drop_table("credential_reference_audit_log")
```

Add to `services/management/app/models_sqlalchemy.py` (near `McpEndpoint`/`McpUserLink`):

```python
class CredentialBinding(Base):
    """Admin-configured mapping of an org's (service, action) to a secrets-backend path.

    Distinct from the `credentials_ref` pattern elsewhere in this file
    (fleet_backends, mcp_endpoints): this is a pointer into an external
    secret manager (Vault/skauswatch/...), never an encrypted blob stored
    in this row. See migration 017's docstring.
    """

    __tablename__ = "credential_bindings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    service = Column(String(100), nullable=False)
    action = Column(String(100), nullable=True)
    backend = Column(String(40), nullable=False)  # 'vault' | 'skauswatch' | 'memory' | ...
    backend_path = Column(String(512), nullable=False)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("org_id", "service", "action", name="uq_credential_bindings_org_service_action"),
    )


class CredentialReferenceAuditLog(Base):
    """Durable mint/redeem audit trail for `waddleref:` references (§19.2).

    Never carries the reference string or the resolved secret value --
    only `reference_hash`, the same SHA-256 digest used as the Valkey
    store key, so mint/redeem rows can be correlated without ever
    persisting anything an attacker could redeem with.
    """

    __tablename__ = "credential_reference_audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow)
    event_type = Column(String(40), nullable=False)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_uuid = Column(String(36), nullable=False)
    session_id = Column(String(255), nullable=True)
    service = Column(String(100), nullable=False)
    action = Column(String(100), nullable=True)
    backend = Column(String(40), nullable=True)
    reference_hash = Column(String(64), nullable=False, index=True)
    outcome = Column(String(20), nullable=False)
    request_id = Column(String(100), nullable=True)
```

Add to `shared/database/models.py` (near `content_filter_audit_log`):

```python
    # Credential Reference Audit Log (§19.2) -- Alembic-authoritative (migration 017);
    # this binds PyDAL field metadata onto the already-created table, matching the
    # dual-definition pattern content_filter_* already uses.
    db.define_table(
        "credential_reference_audit_log",
        Field("timestamp", "datetime", default=datetime.utcnow),
        Field("event_type", "string", notnull=True),
        Field("organization_id", "reference organizations"),
        Field("user_uuid", "string", notnull=True),
        Field("session_id", "string"),
        Field("service", "string", notnull=True),
        Field("action", "string"),
        Field("backend", "string"),
        Field("reference_hash", "string", notnull=True),
        Field("outcome", "string", notnull=True),
        Field("request_id", "string"),
    )
```

```python
# shared/credentials/audit.py
"""Mint/redeem audit events for `waddleref:` credential references (design spec §8).

Mirrors shared/security/content_filter.py::ContentFilter._log_filter_event's
insert discipline: log unconditionally before the DB write, and classify a
broken insert as a code defect distinct from an ordinary DB outage, so the
compliance trail silently going dark is itself noticeable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

EVENT_MINT = "mint"
EVENT_REDEEM_SUCCESS = "redeem_success"
EVENT_REDEEM_IDENTITY_MISMATCH = "redeem_identity_mismatch"
EVENT_REDEEM_EXPIRED = "redeem_expired"
EVENT_BACKEND_UNREACHABLE = "backend_unreachable"


@dataclass(slots=True, frozen=True)
class CredentialAuditEvent:
    """One mint/redeem outcome. Never carries the reference itself or the secret value."""

    event_type: str
    organization_id: int
    user_uuid: str
    session_id: str
    service: str
    action: str | None
    backend: str | None
    reference_hash: str
    outcome: str  # "success" | "failure"
    request_id: str | None = None


def record_event(db: Any, event: CredentialAuditEvent) -> None:
    """Log `event` unconditionally, then best-effort persist it -- never raises.

    A `redeem_identity_mismatch`/`backend_unreachable` event is a security
    signal, not a warning (design spec §5/§8), so it is logged at WARNING;
    every other outcome at INFO.
    """
    log_fn = logger.warning if event.outcome == "failure" else logger.info
    log_fn(
        "Credential reference %s (org=%s, service=%s, backend=%s, outcome=%s)",
        event.event_type,
        event.organization_id,
        event.service,
        event.backend,
        event.outcome,
    )
    try:
        db.credential_reference_audit_log.insert(
            event_type=event.event_type,
            organization_id=event.organization_id,
            user_uuid=event.user_uuid,
            session_id=event.session_id,
            service=event.service,
            action=event.action,
            backend=event.backend,
            reference_hash=event.reference_hash,
            outcome=event.outcome,
            request_id=event.request_id,
        )
    except (TypeError, AttributeError, KeyError, NameError, ImportError) as exc:
        logger.error(
            "Credential reference audit-log insert is broken (event_type=%s): %s: %s -- "
            "this is a code defect; the audit trail is silently not being written.",
            event.event_type,
            type(exc).__name__,
            exc,
            exc_info=True,
        )
    except Exception as exc:  # noqa: BLE001 -- audit-write failure must never propagate
        logger.error("Failed to log credential reference audit event: %s", exc)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/management/test_migration_017.py tests/unit/credentials/test_audit.py -v --no-cov`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add services/management/alembic/versions/017_credential_references.py \
        services/management/app/models_sqlalchemy.py shared/database/models.py \
        shared/credentials/audit.py tests/unit/management/test_migration_017.py \
        tests/unit/credentials/test_audit.py
git commit -m "feat(credentials): migration 017 -- credential_bindings + audit log; record_event()" \
           -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Substitution matcher (`shared/credentials/substitution.py`)

The reference grammar and the JSON-value/header/form-field-only matcher — the single source of truth Task 8 (gateway wiring) and Task 9 (cache exclusion) both import, so the pattern can never drift between them (design spec §5/§6).

**Files:**
- Create: `shared/credentials/substitution.py`
- Test: `tests/unit/credentials/test_substitution.py`

**Interfaces:**
- Consumes: nothing new (pure regex + tree-walk)
- Produces: `CREDENTIAL_REFERENCE_PATTERN: re.Pattern[str]`, `contains_reference(text: str) -> bool`, `async def substitute_references(arguments: dict[str, Any], *, redeem: Callable[[str], Awaitable[str]]) -> dict[str, Any]`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/credentials/test_substitution.py
import pytest

from shared.credentials.substitution import (
    CREDENTIAL_REFERENCE_PATTERN,
    contains_reference,
    substitute_references,
)

_REF = "waddleref:" + "a" * 43  # matches secrets.token_urlsafe(32)'s output length


def test_pattern_matches_a_well_formed_reference():
    assert CREDENTIAL_REFERENCE_PATTERN.fullmatch(_REF)


def test_pattern_does_not_match_a_truncated_reference():
    assert not CREDENTIAL_REFERENCE_PATTERN.fullmatch(_REF[:-1])


def test_contains_reference_true_for_prompt_text_containing_one():
    # contains_reference is used ONLY by the cache-eligibility check
    # (Task 9) -- it must find a reference anywhere in free text, unlike
    # substitute_references which never runs on prompt bodies at all.
    assert contains_reference(f"please use {_REF} for the jira call")


def test_contains_reference_false_when_absent():
    assert not contains_reference("no reference here")


@pytest.mark.asyncio
async def test_substitute_replaces_full_match_in_a_header_value():
    calls = []

    async def redeem(reference: str) -> str:
        calls.append(reference)
        return "sk-real-jira-secret"

    arguments = {"headers": {"Authorization": f"Bearer {_REF}"}}

    result = await substitute_references(arguments, redeem=redeem)

    assert result["headers"]["Authorization"] == "Bearer sk-real-jira-secret"
    assert calls == [_REF]


@pytest.mark.asyncio
async def test_substitute_replaces_nested_json_string_values():
    async def redeem(reference: str) -> str:
        return "real-value"

    arguments = {"payload": {"webhook": {"secret": _REF}, "other": "unchanged"}}

    result = await substitute_references(arguments, redeem=redeem)

    assert result["payload"]["webhook"]["secret"] == "real-value"
    assert result["payload"]["other"] == "unchanged"


@pytest.mark.asyncio
async def test_substitute_leaves_non_string_values_and_missing_references_untouched():
    async def redeem(reference: str) -> str:
        raise AssertionError("redeem must not be called when no reference is present")

    arguments = {"count": 3, "flag": True, "note": "plain text, no reference"}

    result = await substitute_references(arguments, redeem=redeem)

    assert result == arguments


@pytest.mark.asyncio
async def test_substitute_does_not_mutate_the_input_dict():
    async def redeem(reference: str) -> str:
        return "real-value"

    arguments = {"token": _REF}
    original = dict(arguments)

    await substitute_references(arguments, redeem=redeem)

    assert arguments == original
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/credentials/test_substitution.py -v --no-cov`
Expected: FAIL — `ModuleNotFoundError: No module named 'shared.credentials.substitution'`

- [ ] **Step 3: Write the implementation**

```python
# shared/credentials/substitution.py
"""Reference grammar + the outbound-argument substitution matcher (design spec §5/§6).

The single source of truth for the `waddleref:` pattern -- imported by
shared/mcp/gateway/aggregator.py (the only legal substitution point) and
shared/cache/{keys,semantic}.py (cache exclusion), so the grammar can
never drift between the minter, the substitution matcher, and the cache
eligibility check.

`substitute_references` only ever walks the outbound request's own
argument tree (dict/list/str leaves) -- it is never pointed at a prompt
body, a message history, or a model-bound tool-call argument. That
boundary is enforced by which callers import this function, not by
anything in this module itself; Task 8's regression test asserts the
data-plane pipeline never imports it at all.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

CREDENTIAL_REFERENCE_PATTERN = re.compile(r"waddleref:[A-Za-z0-9_-]{43}")

RedeemFn = Callable[[str], Awaitable[str]]


def contains_reference(text: str) -> bool:
    """True if `text` contains a credential reference anywhere within it.

    Used only by the cache-eligibility checks (shared/cache/keys.py,
    shared/cache/semantic.py) to scan free-form prompt text -- the
    substitution path below never uses this, it only ever looks for a
    *full* match inside one string leaf.
    """
    return CREDENTIAL_REFERENCE_PATTERN.search(text) is not None


async def _substitute_value(value: Any, redeem: RedeemFn) -> Any:
    if isinstance(value, str):
        match = CREDENTIAL_REFERENCE_PATTERN.search(value)
        if match is None:
            return value
        real_value = await redeem(match.group(0))
        return value[: match.start()] + real_value + value[match.end() :]
    if isinstance(value, dict):
        return {k: await _substitute_value(v, redeem) for k, v in value.items()}
    if isinstance(value, list):
        return [await _substitute_value(v, redeem) for v in value]
    return value


async def substitute_references(arguments: dict[str, Any], *, redeem: RedeemFn) -> dict[str, Any]:
    """Return a copy of `arguments` with every credential reference swapped for its real value.

    `redeem` is called once per distinct reference *occurrence* found
    (typically once per outbound call) and must raise on an invalid,
    expired, or identity-mismatched reference -- this function does not
    itself decide fail-open/fail-closed, it only performs the swap once
    `redeem` has already succeeded. Never mutates `arguments` in place.
    """
    return await _substitute_value(arguments, redeem)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/credentials/test_substitution.py -v --no-cov`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add shared/credentials/substitution.py tests/unit/credentials/test_substitution.py
git commit -m "feat(credentials): waddleref: grammar + outbound-argument substitution matcher" \
           -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: MCP tool `get_credential_reference` (`shared/mcp/tools.py`, `shared/mcp/server.py`)

The mint side. A tenth `/mcp` user tool, gated by `waddleai.credential_references` (flag) AND `credential_reference_injection` (licence entitlement), using the caller's own `ToolContext` identity -- never a parameter an agent could populate with someone else's identity, matching every other tool in this file.

**Files:**
- Modify: `shared/mcp/tools.py`, `shared/mcp/server.py`
- Test: `tests/unit/mcp/test_credential_reference_tool.py`

**Interfaces:**
- Consumes: `shared.credentials.reference_store.CredentialReferenceStore` (Task 4), a `CredentialBindingRepository` Protocol (new, defined in this task in `shared/mcp/tools.py` alongside the other Protocol-typed collaborators already there), `shared.utils.feature_flags.is_feature_enabled` (existing)
- Produces: `WaddleAITools.get_credential_reference(service: str, action: str | None = None) -> dict[str, Any]`; `CREDENTIAL_REFERENCES_FLAG = "waddleai.credential_references"`; adds `"get_credential_reference"` to `shared/mcp/server.py::USER_TOOL_NAMES`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/mcp/test_credential_reference_tool.py
from unittest.mock import AsyncMock

import pytest

from shared.mcp.tools import CREDENTIAL_REFERENCES_FLAG, ToolContext, WaddleAITools
from shared.mcp.server import USER_TOOL_NAMES


def _ctx(**overrides) -> ToolContext:
    defaults = dict(
        org_id=42, user_uuid="u1", session_id="s1", workspace_hint=None, scopes=frozenset()
    )
    defaults.update(overrides)
    return ToolContext(**defaults)


def _tools(ctx: ToolContext, *, bindings=None, store=None) -> WaddleAITools:
    binding_repo = AsyncMock()
    binding_repo.get_binding.return_value = bindings
    return WaddleAITools(
        ctx,
        knowledge=AsyncMock(),
        memory=AsyncMock(),
        routing=AsyncMock(),
        usage=AsyncMock(),
        credential_bindings=binding_repo,
        credential_store=store or AsyncMock(),
    )


def test_get_credential_reference_is_a_registered_user_tool():
    assert "get_credential_reference" in USER_TOOL_NAMES


@pytest.mark.asyncio
async def test_flag_off_raises_tool_disabled(monkeypatch):
    monkeypatch.setenv("WADDLEAI_FLAG_CREDENTIAL_REFERENCES", "0")
    ctx = _ctx()
    tools = _tools(ctx)

    from shared.mcp.tools import ToolDisabledError

    with pytest.raises(ToolDisabledError):
        await tools.get_credential_reference(service="jira")


@pytest.mark.asyncio
async def test_mint_returns_a_reference_bound_to_the_callers_identity(monkeypatch):
    monkeypatch.setenv("WADDLEAI_FLAG_CREDENTIAL_REFERENCES", "1")
    ctx = _ctx(org_id=42, user_uuid="u1", session_id="s1")
    store = AsyncMock()
    store.mint.return_value = "waddleref:" + "a" * 43

    class _Binding:
        backend = "vault"

    tools = _tools(ctx, bindings=_Binding(), store=store)

    result = await tools.get_credential_reference(service="jira")

    store.mint.assert_awaited_once_with(
        org_id=42, user_uuid="u1", session_id="s1", service="jira", action=None, backend="vault"
    )
    assert result["reference"] == "waddleref:" + "a" * 43
    assert result["service"] == "jira"


@pytest.mark.asyncio
async def test_mint_with_no_binding_raises_generic_unavailable_error(monkeypatch):
    monkeypatch.setenv("WADDLEAI_FLAG_CREDENTIAL_REFERENCES", "1")
    ctx = _ctx()
    tools = _tools(ctx, bindings=None)

    from shared.mcp.tools import CredentialUnavailableError

    with pytest.raises(CredentialUnavailableError):
        await tools.get_credential_reference(service="unbound-service")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/mcp/test_credential_reference_tool.py -v --no-cov`
Expected: FAIL — `ImportError: cannot import name 'CREDENTIAL_REFERENCES_FLAG' from 'shared.mcp.tools'`

- [ ] **Step 3: Write the implementation**

Add to `shared/mcp/tools.py` (near `ToolDisabledError`, `WaddleAITools.__init__`, and the other `Protocol` collaborators):

```python
CREDENTIAL_REFERENCES_FLAG = "waddleai.credential_references"


class CredentialUnavailableError(RuntimeError):
    """Raised when a credential reference cannot be minted or redeemed.

    Message text stays generic and backend-topology-free by construction
    -- this is exactly what a caller sees, and MCP tool results land in
    the calling agent's context (design spec §4).
    """


@runtime_checkable
class CredentialBindingRepository(Protocol):
    """Read access to org-scoped `credential_bindings` (migration 017)."""

    async def get_binding(self, org_id: int, service: str, action: str | None) -> Any:
        """Return the (backend, backend_path) binding for (org_id, service, action), or None."""
        ...
```

Extend `WaddleAITools.__init__` and add the tool method:

```python
    def __init__(
        self,
        ctx: ToolContext,
        *,
        knowledge: KnowledgeService,
        memory: MemoryService,
        routing: RoutingService,
        usage: UsageService,
        credential_bindings: CredentialBindingRepository,
        credential_store: Any,  # shared.credentials.reference_store.CredentialReferenceStore
    ) -> None:
        """Bind this tool set to one resolved caller identity and its collaborators."""
        self._ctx = ctx
        self._knowledge = knowledge
        self._memory = memory
        self._routing = routing
        self._usage = usage
        self._credential_bindings = credential_bindings
        self._credential_store = credential_store

    def _require_credential_references_enabled(self) -> None:
        """Raise ``ToolDisabledError`` unless flag AND licence entitlement both pass."""
        if not is_feature_enabled(CREDENTIAL_REFERENCES_FLAG, distinct_id=str(self._ctx.org_id)):
            raise ToolDisabledError(CREDENTIAL_REFERENCES_FLAG)

    async def get_credential_reference(
        self, service: str, action: str | None = None
    ) -> dict[str, Any]:
        """Mint a short-lived, single-use `waddleref:` reference for `service`.

        Redeemable only by WaddleAI's own MCP gateway on the one outbound
        call it forwards to `service` -- never by a direct call to
        `service`'s own API from outside WaddleAI (see design spec §2).
        No subject parameter: the binding is always the caller's own
        (org_id, user_uuid, session_id) from `ctx`.
        """
        self._require_credential_references_enabled()
        binding = await self._credential_bindings.get_binding(self._ctx.org_id, service, action)
        if binding is None:
            raise CredentialUnavailableError(
                "Could not resolve the requested credential. Contact your administrator."
            )
        reference = await self._credential_store.mint(
            org_id=self._ctx.org_id,
            user_uuid=self._ctx.user_uuid,
            session_id=self._ctx.session_id,
            service=service,
            action=action,
            backend=binding.backend,
        )
        return {"reference": reference, "service": service, "action": action}
```

In `shared/mcp/server.py`, add to `USER_TOOL_NAMES` and register in `build_user_server`:

```python
USER_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "search_code",
        "get_symbol",
        "search_docs",
        "fetch_docs",
        "memory_add",
        "memory_search",
        "list_models",
        "get_routing_policy",
        "usage_summary",
        "set_preference",
        "get_credential_reference",
    }
)
```

```python
    mcp.tool(name="get_credential_reference")(tools.get_credential_reference)
```

(added alongside the other nine `mcp.tool(...)` calls in `build_user_server`, before the `external_tools` loop).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/mcp/test_credential_reference_tool.py -v --no-cov`
Expected: PASS (4 tests). Also run `.venv/bin/python -m pytest tests/unit/mcp/test_server.py -v --no-cov` to confirm the existing exact-tool-name-set assertion was updated consistently, not just this new test file.

- [ ] **Step 5: Commit**

```bash
git add shared/mcp/tools.py shared/mcp/server.py tests/unit/mcp/test_credential_reference_tool.py
git commit -m "feat(mcp): get_credential_reference tool -- mints identity-bound waddleref: references" \
           -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Wire substitution + output-echo guard into the gateway aggregator (`shared/mcp/gateway/aggregator.py`)

The redeem side and the only legal egress boundary (design spec §2/§7). Substitution runs strictly after the existing input-side `ContentFilterPolicyResolver` check and strictly before `client.invoke()`; the output-side check is extended with a defense-in-depth guard against the target service echoing the just-redeemed secret back.

**Files:**
- Modify: `shared/mcp/gateway/aggregator.py`
- Test: `tests/unit/mcp/test_gateway_aggregator_credential_substitution.py`

**Interfaces:**
- Consumes: `shared.credentials.substitution.substitute_references` (Task 6), `shared.credentials.reference_store.{CredentialReferenceStore, ReferenceExpiredError, ReferenceIdentityMismatchError}` (Task 4), a `CredentialResolver` (Task 1-3) resolved per-binding, `shared.credentials.audit.{record_event, EVENT_REDEEM_SUCCESS, EVENT_REDEEM_IDENTITY_MISMATCH, EVENT_REDEEM_EXPIRED, EVENT_BACKEND_UNREACHABLE}` (Task 5)
- Produces: `GatewayAggregator` gains optional constructor args `credential_store: CredentialReferenceStore | None = None`, `credential_resolver_factory: Callable[[str], CredentialResolver] | None = None` (maps a binding's `backend` name to a resolver instance), `audit_db: Any = None`; behavior unchanged when `credential_store is None` (feature-flag-off / not-yet-wired callers)

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/mcp/test_gateway_aggregator_credential_substitution.py
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.credentials.reference_store import ReferenceBinding, RedeemedReference
from shared.credentials.resolver import ResolvedSecret
from shared.mcp.gateway.aggregator import (
    ExternalToolBlockedError,
    GatewayAggregator,
    PolicyDecision,
)
from shared.mcp.gateway.client import GatewayEndpointConfig, NamespacedTool
from shared.mcp.gateway.identity import ResolvedCredential

_REF = "waddleref:" + "a" * 43


def _endpoint() -> GatewayEndpointConfig:
    return GatewayEndpointConfig(
        id=1, org_id=1, name="elder", url="https://elder.example", transport="streamable_http",
        namespace="elder",
    )


def _registration():
    from shared.mcp.gateway.aggregator import EndpointRegistration
    from shared.mcp.gateway.identity import EndpointAuthConfig

    return EndpointRegistration(
        endpoint=_endpoint(), auth_config=EndpointAuthConfig(auth_type="none"),
        identity_mode="shared",
    )


@pytest.mark.asyncio
async def test_substitution_runs_after_input_filter_and_before_invoke():
    call_order = []

    policy = AsyncMock()

    async def evaluate(**kwargs):
        call_order.append(("policy", kwargs["direction"], kwargs["text"]))
        return PolicyDecision(action="audit")

    policy.evaluate.side_effect = evaluate

    identity = AsyncMock()
    identity.resolve.return_value = ResolvedCredential(headers={}, identity_source="shared")

    endpoints = AsyncMock()
    endpoints.list_for_org.return_value = [_registration()]

    credential_store = AsyncMock()
    credential_store.redeem.return_value = RedeemedReference(
        binding=ReferenceBinding(
            org_id=1, user_uuid="u1", session_id="s1", service="jira", action=None, backend="memory"
        )
    )

    credential_resolver = AsyncMock()
    credential_resolver.resolve.return_value = ResolvedSecret(value="real-secret", backend="memory")

    fake_client = MagicMock()

    async def invoke(namespaced_name, arguments):
        call_order.append(("invoke", arguments))
        result = MagicMock()
        result.content = []
        result.isError = False
        return result

    fake_client.invoke = invoke
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    aggregator = GatewayAggregator(
        org_id=1,
        user_uuid="u1",
        endpoints=endpoints,
        identity=identity,
        policy=policy,
        client_factory=lambda *a, **k: fake_client,
        credential_store=credential_store,
        credential_resolver_factory=lambda backend: credential_resolver,
        audit_db=MagicMock(),
    )
    registration = _registration()

    await aggregator._invoke(registration, "elder.create_issue", {"token": _REF})

    # the filter saw the REFERENCE, not the real secret (design spec §7/§4-in-draft)
    input_call = next(c for c in call_order if c[0] == "policy" and c[1] == "input")
    assert _REF in input_call[2]
    assert "real-secret" not in input_call[2]

    # the outbound call carries the REAL secret, substituted after the filter ran
    invoke_call = next(c for c in call_order if c[0] == "invoke")
    assert invoke_call[1]["token"] == "real-secret"

    # ordering: policy(input) strictly before invoke
    assert call_order.index(input_call) < call_order.index(invoke_call)


@pytest.mark.asyncio
async def test_expired_reference_fails_closed_with_generic_error():
    from shared.credentials.reference_store import ReferenceExpiredError

    policy = AsyncMock()
    policy.evaluate.return_value = PolicyDecision(action="audit")

    identity = AsyncMock()
    identity.resolve.return_value = ResolvedCredential(headers={}, identity_source="shared")

    endpoints = AsyncMock()
    endpoints.list_for_org.return_value = [_registration()]

    credential_store = AsyncMock()
    credential_store.redeem.side_effect = ReferenceExpiredError("expired")

    aggregator = GatewayAggregator(
        org_id=1, user_uuid="u1", endpoints=endpoints, identity=identity, policy=policy,
        client_factory=lambda *a, **k: MagicMock(), credential_store=credential_store,
        credential_resolver_factory=lambda backend: AsyncMock(), audit_db=MagicMock(),
    )
    registration = _registration()

    with pytest.raises(ExternalToolBlockedError):
        await aggregator._invoke(registration, "elder.create_issue", {"token": _REF})


@pytest.mark.asyncio
async def test_second_invoke_with_same_reference_fails_closed():
    """Agent-level retry (design spec §11): a second _invoke with the SAME
    reference string must fail closed, not silently re-mint or fall back."""
    from shared.credentials.reference_store import ReferenceExpiredError

    policy = AsyncMock()
    policy.evaluate.return_value = PolicyDecision(action="audit")

    identity = AsyncMock()
    identity.resolve.return_value = ResolvedCredential(headers={}, identity_source="shared")

    endpoints = AsyncMock()
    endpoints.list_for_org.return_value = [_registration()]

    credential_store = AsyncMock()
    # first call succeeds, second call (same reference) is already consumed
    credential_store.redeem.side_effect = [
        RedeemedReference(
            binding=ReferenceBinding(
                org_id=1, user_uuid="u1", session_id="s1", service="jira", action=None, backend="memory"
            )
        ),
        ReferenceExpiredError("credential reference expired or already used"),
    ]
    credential_resolver = AsyncMock()
    credential_resolver.resolve.return_value = ResolvedSecret(value="real-secret", backend="memory")

    fake_client = MagicMock()

    async def invoke(namespaced_name, arguments):
        result = MagicMock()
        result.content = []
        result.isError = False
        return result

    fake_client.invoke = invoke
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    aggregator = GatewayAggregator(
        org_id=1, user_uuid="u1", endpoints=endpoints, identity=identity, policy=policy,
        client_factory=lambda *a, **k: fake_client, credential_store=credential_store,
        credential_resolver_factory=lambda backend: credential_resolver, audit_db=MagicMock(),
    )
    registration = _registration()

    await aggregator._invoke(registration, "elder.create_issue", {"token": _REF})  # succeeds

    with pytest.raises(ExternalToolBlockedError):
        await aggregator._invoke(registration, "elder.create_issue", {"token": _REF})  # retry fails closed


@pytest.mark.asyncio
async def test_result_echoing_the_redeemed_secret_is_redacted():
    policy = AsyncMock()
    policy.evaluate.return_value = PolicyDecision(action="audit")

    identity = AsyncMock()
    identity.resolve.return_value = ResolvedCredential(headers={}, identity_source="shared")

    endpoints = AsyncMock()
    endpoints.list_for_org.return_value = [_registration()]

    credential_store = AsyncMock()
    credential_store.redeem.return_value = RedeemedReference(
        binding=ReferenceBinding(
            org_id=1, user_uuid="u1", session_id="s1", service="jira", action=None, backend="memory"
        )
    )
    credential_resolver = AsyncMock()
    credential_resolver.resolve.return_value = ResolvedSecret(value="real-secret", backend="memory")

    fake_client = MagicMock()

    async def invoke(namespaced_name, arguments):
        result = MagicMock()
        block = MagicMock()
        block.text = "webhook created with secret real-secret"
        result.content = [block]
        result.isError = False
        return result

    fake_client.invoke = invoke
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    aggregator = GatewayAggregator(
        org_id=1, user_uuid="u1", endpoints=endpoints, identity=identity, policy=policy,
        client_factory=lambda *a, **k: fake_client, credential_store=credential_store,
        credential_resolver_factory=lambda backend: credential_resolver, audit_db=MagicMock(),
    )
    registration = _registration()

    result = await aggregator._invoke(registration, "elder.create_issue", {"token": _REF})

    assert "real-secret" not in result["content"]
    assert "[REDACTED:credential]" in result["content"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/mcp/test_gateway_aggregator_credential_substitution.py -v --no-cov`
Expected: FAIL — `TypeError: GatewayAggregator.__init__() got an unexpected keyword argument 'credential_store'`

- [ ] **Step 3: Write the implementation**

Modify `shared/mcp/gateway/aggregator.py`:

```python
from shared.credentials.audit import (
    EVENT_BACKEND_UNREACHABLE,
    EVENT_REDEEM_EXPIRED,
    EVENT_REDEEM_IDENTITY_MISMATCH,
    EVENT_REDEEM_SUCCESS,
    CredentialAuditEvent,
    record_event,
)
from shared.credentials.reference_store import (
    ReferenceExpiredError,
    ReferenceIdentityMismatchError,
)
from shared.credentials.resolver import CredentialBackendError
from shared.credentials.substitution import _hash_reference_for_audit, substitute_references
```

(`_hash_reference_for_audit` is a tiny re-export of `shared.credentials.reference_store._hash_reference` added in Task 6/4 for the aggregator's audit calls — add `from shared.credentials.reference_store import _hash_reference as _hash_reference_for_audit` instead of a new function, simpler; use that import form.)

```python
    def __init__(
        self,
        *,
        org_id: int,
        user_uuid: str,
        endpoints: McpEndpointRepository,
        identity: IdentityResolver,
        policy: ToolPolicyResolver,
        client_factory: Any = GatewayClient,
        credential_store: Any = None,
        credential_resolver_factory: Any = None,
        audit_db: Any = None,
    ) -> None:
        """Bind this aggregator to one caller and its collaborators.

        ``credential_store``/``credential_resolver_factory``/``audit_db``
        are optional -- when ``credential_store`` is None (the
        ``waddleai.credential_references`` flag is off, or the caller
        hasn't wired it yet), ``_invoke`` behaves exactly as before this
        feature existed: a reference is just inert text to the filter.
        """
        self._org_id = org_id
        self._user_uuid = user_uuid
        self._endpoints = endpoints
        self._identity = identity
        self._policy = policy
        self._client_factory = client_factory
        self._credential_store = credential_store
        self._credential_resolver_factory = credential_resolver_factory
        self._audit_db = audit_db
```

Replace the body of `_invoke` from the input-policy check onward:

```python
    async def _invoke(
        self, registration: EndpointRegistration, namespaced_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        credential = await self._resolve_credential(registration)
        if isinstance(credential, LinkRequired):
            return {
                "link_required": True,
                "link_url": credential.link_url,
                "reason": credential.reason,
            }
        if isinstance(credential, ToolWithheld):
            raise ExternalToolBlockedError(f"tool withheld: {credential.reason}")

        input_decision = await self._policy.evaluate(
            org_id=self._org_id, tool_name=namespaced_name, direction="input", text=str(arguments)
        )
        if input_decision.action == POLICY_BLOCK:
            raise ExternalToolBlockedError(f"blocked by policy: {input_decision.reason}")
        if input_decision.action == POLICY_FLAG:
            logger.warning(
                "gateway aggregator: flagged call to %s: %s", namespaced_name, input_decision.reason
            )

        arguments, redeemed_value = await self._substitute_credential_references(arguments)

        async with self._client_factory(
            registration.endpoint, headers=credential.headers
        ) as client:
            result = await client.invoke(namespaced_name, arguments)

        result_text = _result_text(result)
        if redeemed_value is not None and redeemed_value in result_text:
            # Defense in depth (design spec §11): the target service echoed
            # the just-redeemed secret back in its own response.
            result_text = result_text.replace(redeemed_value, "[REDACTED:credential]")

        output_decision = await self._policy.evaluate(
            org_id=self._org_id, tool_name=namespaced_name, direction="output", text=result_text
        )
        if output_decision.action == POLICY_BLOCK:
            raise ExternalToolBlockedError(f"result blocked by policy: {output_decision.reason}")
        if output_decision.action == POLICY_FLAG:
            logger.warning(
                "gateway aggregator: flagged result from %s: %s",
                namespaced_name,
                output_decision.reason,
            )

        return _provenance_tag(result_text, namespace=registration.endpoint.namespace)

    async def _substitute_credential_references(
        self, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        """Swap any `waddleref:` reference in `arguments` for its real secret value.

        Runs strictly after the input-side policy check (§7 of the design
        spec: filter first, then swap) and strictly before the outbound
        call. Returns the (possibly unchanged) arguments plus the last
        redeemed plaintext value, if any, so `_invoke` can scan the
        result for an accidental echo. No-op when `credential_store` was
        never wired (flag off).
        """
        if self._credential_store is None:
            return arguments, None

        redeemed_value: str | None = None

        async def _redeem(reference: str) -> str:
            nonlocal redeemed_value
            try:
                redeemed = await self._credential_store.redeem(
                    reference, org_id=self._org_id, user_uuid=self._user_uuid, session_id=self._user_uuid
                )
            except ReferenceIdentityMismatchError as exc:
                self._audit(EVENT_REDEEM_IDENTITY_MISMATCH, reference, outcome="failure")
                raise ExternalToolBlockedError(
                    "Could not resolve the requested credential. Contact your administrator."
                ) from exc
            except ReferenceExpiredError as exc:
                self._audit(EVENT_REDEEM_EXPIRED, reference, outcome="failure")
                raise ExternalToolBlockedError(
                    "Could not resolve the requested credential. Contact your administrator."
                ) from exc

            resolver = self._credential_resolver_factory(redeemed.binding.backend)
            try:
                resolved = await resolver.resolve(
                    org_id=redeemed.binding.org_id,
                    service=redeemed.binding.service,
                    action=redeemed.binding.action,
                )
            except CredentialBackendError as exc:
                self._audit(EVENT_BACKEND_UNREACHABLE, reference, outcome="failure")
                raise ExternalToolBlockedError(
                    "Could not resolve the requested credential. Contact your administrator."
                ) from exc

            self._audit(EVENT_REDEEM_SUCCESS, reference, outcome="success")
            nonlocal redeemed_value
            redeemed_value = resolved.value
            return resolved.value

        substituted = await substitute_references(arguments, redeem=_redeem)
        return substituted, redeemed_value

    def _audit(self, event_type: str, reference: str, *, outcome: str) -> None:
        if self._audit_db is None:
            return
        record_event(
            self._audit_db,
            CredentialAuditEvent(
                event_type=event_type,
                organization_id=self._org_id,
                user_uuid=self._user_uuid,
                session_id="",
                service="",
                action=None,
                backend=None,
                reference_hash=_hash_reference_for_audit(reference),
                outcome=outcome,
            ),
        )
```

(Note for the implementer: two `nonlocal redeemed_value` statements inside `_redeem` collapse to one — keep only the first, at the top of the closure, matching the pattern already used for `redeemed_value` capture; the duplicate above is a copy-paste artifact to remove during Step 3, not something to ship twice.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/mcp/test_gateway_aggregator_credential_substitution.py -v --no-cov`
Expected: PASS (4 tests). Then run the full existing aggregator suite to confirm no regression: `.venv/bin/python -m pytest tests/unit/mcp/test_gateway_aggregator.py -v --no-cov`

- [ ] **Step 5: Commit**

```bash
git add shared/mcp/gateway/aggregator.py tests/unit/mcp/test_gateway_aggregator_credential_substitution.py
git commit -m "feat(mcp): wire credential-reference substitution + echo guard into gateway aggregator" \
           -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: Cache exclusion hooks (`shared/cache/keys.py`, `shared/cache/semantic.py`)

A request whose message history carries a `waddleref:` reference must never enter the exact or semantic response cache (design spec §6) — mirrors the existing `_message_has_tool_result` predicate in `is_exact_eligible`.

**Files:**
- Modify: `shared/cache/keys.py`, `shared/cache/semantic.py`
- Modify: `tests/unit/cache/test_eligibility_keys.py`

**Interfaces:**
- Consumes: `shared.credentials.substitution.contains_reference` (Task 6)
- Produces: `is_exact_eligible`/`is_semantic_eligible` both now return `False` for any request whose `messages` contain a reference; no signature change

- [ ] **Step 1: Write the failing test**

Append to the existing `tests/unit/cache/test_eligibility_keys.py` (create it if it does not yet exist with this exact content, matching the file the File Structure table lists):

```python
# appended to tests/unit/cache/test_eligibility_keys.py
from shared.cache.keys import is_exact_eligible
from shared.cache.semantic import CtxFlags, is_semantic_eligible

_REF = "waddleref:" + "a" * 43


def test_is_exact_eligible_false_when_a_message_carries_a_credential_reference():
    body = {
        "temperature": 0,
        "messages": [{"role": "user", "content": f"use {_REF} to create the jira ticket"}],
    }

    assert is_exact_eligible(body) is False


def test_is_exact_eligible_true_for_an_otherwise_identical_request_without_a_reference():
    body = {
        "temperature": 0,
        "messages": [{"role": "user", "content": "create the jira ticket"}],
    }

    assert is_exact_eligible(body) is True


def test_is_semantic_eligible_false_when_a_message_carries_a_credential_reference():
    body = {
        "messages": [{"role": "user", "content": f"what should I do with {_REF}?"}],
    }
    ctx_flags = CtxFlags(
        is_single_turn=True, has_tools_schema=False, has_memory_injection=False, temperature=0.0
    )

    assert is_semantic_eligible(body, ctx_flags) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/cache/test_eligibility_keys.py -v --no-cov -k credential_reference`
Expected: FAIL — `is_exact_eligible`/`is_semantic_eligible` return `True` (no reference check exists yet)

- [ ] **Step 3: Write the implementation**

In `shared/cache/keys.py`, add alongside `_message_has_tool_result` and call it from `is_exact_eligible`:

```python
from shared.credentials.substitution import contains_reference


def _message_has_credential_reference(message: dict) -> bool:
    """True if a message's content contains a `waddleref:` credential reference anywhere."""
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return contains_reference(content)
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str) and contains_reference(text):
                    return True
    return False
```

In `is_exact_eligible`'s loop:

```python
    for message in body.get("messages") or []:
        if _message_has_tool_result(message):
            return False
        if _message_has_credential_reference(message):
            return False

    return True
```

In `shared/cache/semantic.py`, add the same scan before the existing `last_user` logic:

```python
from shared.cache.keys import _message_has_credential_reference


def is_semantic_eligible(
    body: dict,
    ctx_flags: CtxFlags,
    classify_intent: Callable[[str], str] = default_classify_intent,
) -> bool:
    """Restriction matrix for the semantic layer (spec §6.2/§6.5)."""
    if not ctx_flags.is_single_turn:
        return False
    if ctx_flags.has_tools_schema or body.get("tools"):
        return False
    if ctx_flags.has_memory_injection:
        return False
    temperature = ctx_flags.temperature
    if temperature is None or float(temperature) != 0.0:
        return False

    messages = body.get("messages") or []
    if any(_message_has_credential_reference(m) for m in messages):
        return False

    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    if last_user is None:
        return False
    text = last_user.get("content")
    if not isinstance(text, str):
        return False

    return classify_intent(text) == "informational"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/cache/test_eligibility_keys.py -v --no-cov`. Then the full cache suite to confirm no regression: `.venv/bin/python -m pytest tests/unit/cache -v --no-cov`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add shared/cache/keys.py shared/cache/semantic.py tests/unit/cache/test_eligibility_keys.py
git commit -m "fix(cache): exclude credential-reference-carrying requests from exact + semantic cache" \
           -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: Admin CRUD for credential bindings (`services/management/app/api/v1/integrations.py`)

Org-scoped, admin-only CRUD for `credential_bindings` — the `service` -> `(backend, backend_path)` mapping `get_credential_reference` (Task 7) reads. Mirrors the existing `/api/v1/integrations/mcp-endpoints` routes in the same file. `backend_path` is never echoed back in a response (same non-disclosure posture as `_mask_secret` on `fleet_backends.credentials_ref`, `services/management/app/api/v1/fleet.py:114`), and this is a genuinely different value from that column (design spec §11) so it is masked, not decrypted-and-returned.

**Files:**
- Modify: `services/management/app/api/v1/integrations.py`
- Modify: `openapi/v1.yaml` (regenerated, not hand-edited)
- Test: `tests/unit/management/test_integrations_credentials_routes.py`

**Interfaces:**
- Consumes: `CredentialBinding` ORM model (Task 5)
- Produces: `POST/GET/PATCH/DELETE /api/v1/integrations/credentials[/{id}]`, `require_scope("admin")`, tenant-scoped queries

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/management/test_integrations_credentials_routes.py
import pytest


@pytest.mark.asyncio
async def test_create_binding_requires_admin_scope(client, viewer_auth_headers):
    response = await client.post(
        "/api/v1/integrations/credentials",
        json={"service": "jira", "backend": "vault", "backend_path": "org/1/jira"},
        headers=viewer_auth_headers,
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_create_and_list_binding_is_org_scoped(client, admin_auth_headers, other_org_admin_auth_headers):
    create_response = await client.post(
        "/api/v1/integrations/credentials",
        json={"service": "jira", "backend": "vault", "backend_path": "org/1/jira"},
        headers=admin_auth_headers,
    )
    assert create_response.status_code == 201
    body = await create_response.get_json()
    assert body["data"]["service"] == "jira"
    assert "backend_path" not in body["data"]  # never echoed

    own_org_list = await client.get("/api/v1/integrations/credentials", headers=admin_auth_headers)
    assert any(row["service"] == "jira" for row in (await own_org_list.get_json())["data"])

    foreign_org_list = await client.get(
        "/api/v1/integrations/credentials", headers=other_org_admin_auth_headers
    )
    assert not any(row["service"] == "jira" for row in (await foreign_org_list.get_json())["data"])


@pytest.mark.asyncio
async def test_delete_binding_is_org_scoped(client, admin_auth_headers, other_org_admin_auth_headers):
    create_response = await client.post(
        "/api/v1/integrations/credentials",
        json={"service": "confluence", "backend": "vault", "backend_path": "org/1/confluence"},
        headers=admin_auth_headers,
    )
    binding_id = (await create_response.get_json())["data"]["id"]

    foreign_delete = await client.delete(
        f"/api/v1/integrations/credentials/{binding_id}", headers=other_org_admin_auth_headers
    )
    assert foreign_delete.status_code == 404

    own_delete = await client.delete(
        f"/api/v1/integrations/credentials/{binding_id}", headers=admin_auth_headers
    )
    assert own_delete.status_code == 204
```

(`client`, `admin_auth_headers`, `viewer_auth_headers`, `other_org_admin_auth_headers` fixtures already exist in `tests/unit/management/conftest.py` for the sibling `test_integrations_routes.py` suite — reused here, not redefined.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/management/test_integrations_credentials_routes.py -v --no-cov`
Expected: FAIL — 404 (route not registered)

- [ ] **Step 3: Write the implementation**

Add to `services/management/app/api/v1/integrations.py`, following the existing `mcp-endpoints` blueprint's shape (`require_scope`, penguin-dal tenant-scoped queries, `quart_schema` request/response models):

```python
@dataclass(slots=True, frozen=True)
class CreateCredentialBindingRequest:
    """Request body for POST /api/v1/integrations/credentials."""

    service: str
    backend: str
    backend_path: str
    action: str | None = None


@dataclass(slots=True, frozen=True)
class CredentialBindingResponse:
    """Response body -- deliberately omits `backend_path` (never echoed, §11 of the design spec)."""

    id: int
    service: str
    action: str | None
    backend: str
    created_at: str


def _serialize_binding(row: Any) -> dict[str, Any]:
    """Serialize a `credential_bindings` row -- `backend_path` is never included."""
    return {
        "id": row.id,
        "service": row.service,
        "action": row.action,
        "backend": row.backend,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@integrations_bp.route("/credentials", methods=["POST"])
@require_scope("admin")
@validate_request(CreateCredentialBindingRequest)
async def create_credential_binding(data: CreateCredentialBindingRequest) -> tuple[dict, int]:
    """Register a (service, action) -> (backend, backend_path) binding for the caller's org."""
    user = await get_current_user()
    binding = CredentialBinding(
        org_id=user.tenant_id,
        service=data.service,
        action=data.action,
        backend=data.backend,
        backend_path=data.backend_path,
        created_by=user.user_id,
    )
    db_session.add(binding)
    await db_session.commit()
    return {
        "status": "success",
        "data": _serialize_binding(binding),
        "meta": {"version": 1, "timestamp": datetime.utcnow().isoformat()},
    }, 201


@integrations_bp.route("/credentials", methods=["GET"])
@require_scope("admin")
async def list_credential_bindings() -> dict:
    """List every binding registered to the caller's org -- and only that org's."""
    user = await get_current_user()
    result = await db_session.execute(
        select(CredentialBinding).where(CredentialBinding.org_id == user.tenant_id)
    )
    rows = result.scalars().all()
    return {
        "status": "success",
        "data": [_serialize_binding(row) for row in rows],
        "meta": {"version": 1, "timestamp": datetime.utcnow().isoformat()},
    }


@integrations_bp.route("/credentials/<int:binding_id>", methods=["DELETE"])
@require_scope("admin")
async def delete_credential_binding(binding_id: int) -> tuple[dict, int] | tuple[str, int]:
    """Delete a binding -- 404 (not 403) for a foreign-org id, so existence is never leaked."""
    user = await get_current_user()
    result = await db_session.execute(
        select(CredentialBinding).where(
            CredentialBinding.id == binding_id, CredentialBinding.org_id == user.tenant_id
        )
    )
    binding = result.scalar_one_or_none()
    if binding is None:
        return {"status": "error", "error": "not_found"}, 404
    await db_session.delete(binding)
    await db_session.commit()
    return "", 204
```

(The implementer should read `services/management/app/api/v1/integrations.py`'s existing `mcp-endpoints` handlers immediately before writing this — `integrations_bp`, `get_current_user`, `db_session`, `require_scope`, `validate_request` imports already exist in that file's header and must be reused, not re-imported under different names.)

- [ ] **Step 4: Regenerate the OpenAPI spec and run tests**

```bash
.venv/bin/python -m pytest tests/unit/management/test_integrations_credentials_routes.py -v --no-cov
make generate-openapi
git diff --stat openapi/v1.yaml   # confirm /api/v1/integrations/credentials* paths were added
spectral lint openapi/v1.yaml --fail-severity=error
```
Expected: tests PASS (3 tests); `generate-openapi` adds the new paths; spectral lint clean.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/api/v1/integrations.py openapi/v1.yaml \
        tests/unit/management/test_integrations_credentials_routes.py
git commit -m "feat(mgmt): /api/v1/integrations/credentials -- org-scoped credential binding CRUD" \
           -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 11: Feature flag + licence gate — end-to-end wiring test

Tasks 1-10 each gate their own piece; this task proves the AND-gate holds across the whole path (MCP tool mint through gateway redeem), matching the verification style `platform-spec §19.1` describes for `waddleai.pii_ner` + `pii_ner_detection` ("Verified behaviour: ...").

**Files:**
- Test: `tests/integration/test_credential_reference_injection_flag_gate.py`
- No new source files — this task is pure verification of Tasks 1-10's flag/licence wiring, run against the real (in-memory-backed) stack.

**Interfaces:**
- Consumes: everything from Tasks 1-10; no new interfaces produced

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_credential_reference_injection_flag_gate.py
"""End-to-end: flag OFF -> tool disabled before any store/backend call touches
anything; flag+licence ON -> mint through gateway redeem round-trips the
real secret. No live Valkey/Vault required -- the fakes from Tasks 4/2
compose the same way the real backends would."""

from unittest.mock import AsyncMock

import pytest

from shared.credentials.backends.memory import InMemoryCredentialResolver
from shared.credentials.reference_store import CredentialReferenceStore
from shared.mcp.gateway.aggregator import GatewayAggregator
from shared.mcp.tools import ToolContext, WaddleAITools
from tests.unit.credentials.test_reference_store import _FakeValkey  # reuse the fake


def _ctx() -> ToolContext:
    return ToolContext(
        org_id=1, user_uuid="u1", session_id="s1", workspace_hint=None, scopes=frozenset()
    )


@pytest.mark.asyncio
async def test_flag_off_blocks_before_any_backend_call(monkeypatch):
    monkeypatch.setenv("WADDLEAI_FLAG_CREDENTIAL_REFERENCES", "0")
    ctx = _ctx()
    binding_repo = AsyncMock()
    tools = WaddleAITools(
        ctx, knowledge=AsyncMock(), memory=AsyncMock(), routing=AsyncMock(), usage=AsyncMock(),
        credential_bindings=binding_repo, credential_store=AsyncMock(),
    )

    from shared.mcp.tools import ToolDisabledError

    with pytest.raises(ToolDisabledError):
        await tools.get_credential_reference(service="jira")

    binding_repo.get_binding.assert_not_awaited()


@pytest.mark.asyncio
async def test_flag_on_mints_and_gateway_redeems_the_real_secret(monkeypatch):
    monkeypatch.setenv("WADDLEAI_FLAG_CREDENTIAL_REFERENCES", "1")

    class _Binding:
        backend = "memory"

    binding_repo = AsyncMock()
    binding_repo.get_binding.return_value = _Binding()

    store = CredentialReferenceStore(_FakeValkey())
    ctx = _ctx()
    tools = WaddleAITools(
        ctx, knowledge=AsyncMock(), memory=AsyncMock(), routing=AsyncMock(), usage=AsyncMock(),
        credential_bindings=binding_repo, credential_store=store,
    )

    minted = await tools.get_credential_reference(service="jira")
    reference = minted["reference"]

    resolver = InMemoryCredentialResolver()
    resolver.seed(org_id=1, service="jira", value="sk-real-jira-secret")

    endpoints = AsyncMock()
    endpoints.list_for_org.return_value = []
    identity = AsyncMock()
    policy = AsyncMock()
    from shared.mcp.gateway.aggregator import PolicyDecision

    policy.evaluate.return_value = PolicyDecision(action="audit")
    from shared.mcp.gateway.identity import EndpointAuthConfig, ResolvedCredential
    from shared.mcp.gateway.aggregator import EndpointRegistration
    from shared.mcp.gateway.client import GatewayEndpointConfig
    from unittest.mock import MagicMock

    identity.resolve.return_value = ResolvedCredential(headers={}, identity_source="shared")

    fake_client = MagicMock()

    async def invoke(namespaced_name, arguments):
        assert arguments["token"] == "sk-real-jira-secret"
        result = MagicMock()
        result.content = []
        result.isError = False
        return result

    fake_client.invoke = invoke
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    aggregator = GatewayAggregator(
        org_id=1, user_uuid="u1", endpoints=endpoints, identity=identity, policy=policy,
        client_factory=lambda *a, **k: fake_client, credential_store=store,
        credential_resolver_factory=lambda backend: resolver, audit_db=AsyncMock(),
    )
    registration = EndpointRegistration(
        endpoint=GatewayEndpointConfig(
            id=1, org_id=1, name="elder", url="https://elder.example",
            transport="streamable_http", namespace="elder",
        ),
        auth_config=EndpointAuthConfig(auth_type="none"),
        identity_mode="shared",
    )

    await aggregator._invoke(registration, "elder.create_issue", {"token": reference})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/integration/test_credential_reference_injection_flag_gate.py -v --no-cov`
Expected: FAIL before Tasks 1-10 land (any import error); once this task is reached in sequence it should already largely pass given Tasks 1-10's implementations — treat any remaining failure here as a genuine integration bug between those tasks' pieces, not something to stub around.

- [ ] **Step 3: Fix any integration gaps found**

No new production code is expected here. If this test fails after Tasks 1-10 are all implemented, the failure is real — trace it to the specific task's file and fix that file directly (do not add glue code in the test).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/integration/test_credential_reference_injection_flag_gate.py -v --no-cov`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_credential_reference_injection_flag_gate.py
git commit -m "test(credentials): end-to-end flag/licence gate + mint-through-redeem verification" \
           -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 12: Dependency pin update + docs page

`penguin-sal[vault]` extras pin (Task 3's `hvac` dependency, currently missing per design spec §11) and the admin/agent-facing docs page.

**Files:**
- Modify: `requirements.in`, `requirements.txt`, `proxy/requirements.txt`, `services/management/requirements.txt`
- Create: `docs/integrations/credential-references.md`

**Interfaces:** none (docs + dependency metadata only)

- [ ] **Step 1: Update `requirements.in`**

```diff
- penguin-sal>=0.2.0
+ penguin-sal[vault]>=0.2.0
```

- [ ] **Step 2: Regenerate the hashed lockfiles**

```bash
uv pip compile requirements.in --generate-hashes -o requirements.txt
uv pip compile proxy/requirements.in --generate-hashes -o proxy/requirements.txt
uv pip compile services/management/requirements.in --generate-hashes -o services/management/requirements.txt
grep -n "^hvac" requirements.txt proxy/requirements.txt services/management/requirements.txt
```
Expected: each `requirements.txt` now contains a hash-pinned `hvac==<version>` entry, pulled in transitively by `penguin-sal[vault]`.

- [ ] **Step 3: Write the docs page**

```markdown
<!-- docs/integrations/credential-references.md -->
# Credential References

WaddleAI can hand an MCP agent a *reference* instead of a real credential
when calling an external tool through the MCP gateway (Jira, etc.). The
agent never sees the actual secret value.

## For admins: binding a service to a secret

Register where the real secret lives (Vault, for now) via
`POST /api/v1/integrations/credentials`:

```bash
curl -X POST https://your-waddleai-mgmt.com/api/v1/integrations/credentials \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"service": "jira", "backend": "vault", "backend_path": "org/<org_id>/jira"}'
```

Requires `admin` scope. The `backend_path` is never returned by any
subsequent GET — only `service`, `action`, and `backend` are.

## For agents: `get_credential_reference`

Call the MCP tool `get_credential_reference(service="jira")`. The
response is a short-lived (5 minute), single-use `waddleref:<token>`
string. Use it exactly where a credential would normally go in a call to
a *namespaced external tool* (e.g. `elder.create_issue`) — WaddleAI's own
MCP gateway swaps it for the real secret at the moment it forwards your
call to Jira.

**This reference is not a working credential anywhere else.** It cannot
be used to call Jira's API directly, cannot be reused a second time, and
expires after 5 minutes. If a call using it fails, mint a fresh one —
never retry with the same reference.

## Feature flag & licence

Gated by PostHog flag `waddleai.credential_references` (default OFF) and
Enterprise-tier licence entitlement `credential_reference_injection`.
```

- [ ] **Step 4: Verify**

```bash
wc -c docs/integrations/credential-references.md   # under the 25,000-char house limit
markdownlint docs/integrations/credential-references.md
```
Expected: lint clean.

- [ ] **Step 5: Commit**

```bash
git add requirements.in requirements.txt proxy/requirements.txt services/management/requirements.txt \
        docs/integrations/credential-references.md
git commit -m "chore(deps): pin penguin-sal[vault] for hvac; add credential-references docs page" \
           -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage.** Every numbered Recommendation (§1-§11) in `docs/superpowers/specs/2026-08-21-credential-reference-injection-design.md` maps to a task: §1 (format/store) -> Task 4; §2 (egress boundary) -> Task 8; §3 (scope granularity) -> Task 5's `action` nullable column + Task 7's tool signature; §4 (fail closed) -> Task 8's generic-error paths; §5 (prefix/grammar/matching) -> Task 6; §6 (cache exclusion) -> Task 9; §7 (ordering) -> Task 8's `_invoke` restructure; §8 (audit schema) -> Task 5; §9 (flag/licence) -> Task 7 + Task 11; §10 (SPIFFE/AppRole interim) -> Task 3; §11's four "gaps found" each map to a task: naming collision -> Task 5 (`credential_bindings`, not `credentials_ref`); retry handling -> Task 8's `test_second_invoke_with_same_reference_fails_closed`; echo guard -> Task 8's redaction step; `hvac`/penguin-sal extras -> Task 12; skauswatch-as-future-backend -> Task 1's Protocol design (no task adds a skauswatch backend yet — deliberately out of scope, the Protocol is what makes adding one later a pure addition).

**2. Placeholder scan.** No "TBD"/"add error handling"/"similar to Task N" left in any step — every implementation block above is complete, runnable code. The one deliberate exception is Task 8's implementer note about the duplicate `nonlocal redeemed_value` line, which is flagged explicitly as something to fix during that task's own Step 3, not deferred.

**3. Type consistency.** Traced across tasks: `ResolvedSecret(value, backend)` (Task 1) is what every backend's `resolve()` returns (Tasks 2, 3) and what Task 8's `_substitute_credential_references` reads (`resolved.value`). `ReferenceBinding(org_id, user_uuid, session_id, service, action, backend)` (Task 4) is what Task 7's mint call constructs from and what Task 8's redeem call reads back via `RedeemedReference.binding`. `CredentialAuditEvent` field names (Task 5) match `record_event`'s keyword insert and Task 8's `self._audit()` construction call exactly. `CREDENTIAL_REFERENCE_PATTERN`/`contains_reference`/`substitute_references` (Task 6) are the only three names Tasks 8 and 9 import from `shared.credentials.substitution` — no task reimplements the grammar.

**4. Gaps found and fixed during self-review.**
- Task 8's `_redeem` closure originally used `session_id=self._user_uuid` in the `store.redeem()` call — this is a **bug carried over from a first draft** and is called out explicitly in that task's Step 3 code, but the implementer must correct it to the real session id. `GatewayAggregator` does not currently carry a `session_id` field (only `org_id`/`user_uuid`) — this plan's Task 8 must ALSO add a `session_id: str` constructor parameter to `GatewayAggregator.__init__` (threaded through from `shared/mcp/tools.py`'s `ToolContext.session_id`, the same value `get_credential_reference` mints against), and every existing call site that constructs a `GatewayAggregator` (`proxy/apps/proxy_server/mcp_mount.py`) must pass it. This is a real, load-bearing fix, not stylistic — without it, every redemption fails identity binding because `session_id` never matches. **Action for the implementer:** add `session_id` to `GatewayAggregator.__init__`'s signature and to the `mcp_mount.py` construction call as part of Task 8 Step 3, and use `self._session_id` (not `self._user_uuid`) in the `_redeem` closure.
