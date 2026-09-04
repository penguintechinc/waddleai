# Provider Destination Failover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task is self-contained: read only your task plus the **Global Constraints**, **File Structure**, and **Execution Waves** sections — you should not need the full design spec.

**Goal:** Give an org active/standby destinations for the *same* logical model, each served from its own (BYOK) credential, with retryable-only failover, a per-destination circuit breaker, and Enterprise flag+entitlement gating — the existing dispatch path stays byte-for-byte identical when the feature is gated off.

**Architecture:** `RoutingStage` still picks the logical model. A new branch in `DispatchStage` (proxy) resolves an ordered destination list for `(org, model)` via `DestinationResolver` (one parameterized SQL read), builds per-credential connectors through `DestinationConnectorRegistry` (reusing the existing connector classes; secrets live only inside the connector), and walks the list in `FailoverDispatcher` — retryable failure → next destination, client error (4xx) → surface, breaker-open → skip. Credentials are tenant-owned rows in `provider_credentials.owner_org_id` and are excluded from the platform pool. Management exposes REST CRUD for destinations + BYOK credentials, Enterprise-gated.

**Tech Stack:** Python 3.13, async/await; Quart + penguin-dal + quart-schema (management); raw PyDAL / `executesql` (proxy read path); Alembic (schema); the existing `shared/utils/llm_connectors.py` connector classes + `ProviderError` taxonomy; `ProviderStats`-style breaker; PostHog feature flags + `penguin_licensing`; Prometheus (`WaddleAIMetrics`); pytest (unit + in-process aiohttp stubs).

**Spec:** `docs/superpowers/specs/2026-09-04-provider-destination-failover-design.md` (owner-approved, security-reviewed). This plan implements §3–§8 + docs/release-notes (§9 Phase 1). It does **not** redesign the spec; the two deliberate deviations are documented inline (resolver reads via `executesql` not a PyDAL mirror; the new attempt-record type is named `DestinationAttempt` to avoid colliding with the existing `AttemptRecord` in `llm_connectors.py`).

## Global Constraints

Every task's requirements implicitly include this section. Exact values, copied from the spec and house rules.

**Security invariants (each has a named test — a task touching one is labelled "(security-critical)"):**

| # | Invariant | Enforced at |
|---|---|---|
| S1 | `org_id` comes only from the authenticated identity (JWT/API-key org); never body/query (except `PROVIDER_ADMIN` cross-org, then 403 on mismatch) | management routes, `DispatchStage` |
| S2 | A destination can reference only a credential owned by the same org or the platform pool | write 422, resolve SQL, registry assert |
| S3 | Tenant-owned credentials are excluded from the platform pool selector | `_select_credential` (mutation-proven) |
| S4 | Credential material is never returned, logged, cached outside the connector, or included in `repr` | DTOs, registry, `Destination` |
| S5 | Failover only on retryable failures; 4xx never fails over and never trips the breaker | dispatcher matrix test |
| S6 | Failover only before the first flushed byte | `bytes_flushed` test |
| S7 | Bounded attempts (≤5) and bounded per-attempt time | API cap + `wait_for` tests |
| S8 | Cache keys include `org_id`; org A's destinations are never served to org B | resolver cross-org test |
| S9 | `ctx.provider_pin` and `ctx.local_only` (both written by `RoutingStage` from `RouteDecision`) restrict eligible destinations | RoutingStage + resolver tests |
| S10 | Flag OFF or entitlement absent ⇒ behaviour identical to today, no new SQL on the hot path | gate test |
| S11 | The security_v2 upstream filter (pseudonymise → de-pseudonymise → cleanup) applies on the failover branch exactly as on the existing branch | DispatchStage test with filter enabled |
| S12 | Platform credential endpoints never expose or mutate tenant-owned rows | providers route tests |

**Names, gates, and bounds (copied verbatim):**

- **Feature flag:** `waddleai.provider_failover`, default **OFF**, fail-safe; env override `WADDLEAI_FLAG_PROVIDER_FAILOVER`.
- **Enterprise entitlement:** `waddleai_provider_failover` via `penguin_licensing` `check_feature(...)`, **fail-closed**; domain bypass unchanged.
- **Gate is two-layer.** Management: flag OFF → **404**, entitlement absent → **403**, fail-closed on any evaluation error. Proxy: flag OFF or entitlement absent ⇒ **degrade to the existing path**, never error; memoised per org for **60 s**; counted `waddleai_destination_gate_denied_total{reason}`.
- **`owner_org_id`, never `org_id`.** The tenant column added to `provider_credentials` is `owner_org_id` (`Integer` FK `organizations.id`). The pre-existing `org_id` column (`String`, `models_sqlalchemy.py:154`) is the **provider's** workspace id and is unrelated. Every pool predicate and every resolver/registry query references `owner_org_id`; a unit test asserts `org_id` is never used for tenancy.
- **≤5 enabled destinations per `(org, model)`** — API-enforced (returns 422 on the 6th). `UNIQUE (organization_id, model, priority)`.
- **Timeout default 30 s** when `timeout_seconds` is NULL; column CHECK `1..600`. Bounds *total time* (non-streaming) and *time-to-first-chunk* (streaming, `asyncio.wait_for` on the first `__anext__`).
- **Retryable taxonomy** — retryable (fail over, trip breaker): `ProviderTimeoutError`, `ProviderRateLimitError`, `ProviderServerError` (timeout, connection error, 429 incl. Bedrock `ThrottlingException`, 5xx incl. Anthropic 529/503). Non-retryable (never fail over, never trip breaker): `ProviderClientError` (400/401/403/404/413/422).
- **Gated-off = byte-for-byte unchanged.** When `failover_enabled(org)` is false the failover branch is not entered, no destination SQL runs, and `DispatchStage`/`main.py` behaviour is identical to today. Regression asserted by S10.
- **House rules:** `from __future__ import annotations` at the top of every new module; `@dataclass(slots=True)` on every data structure (`frozen=True` where it must not mutate); PEP 257 docstring (first-line summary + 1–2 lines context) on every class/function; async/await throughout; blocking penguin-dal / boto3 calls via `asyncio.to_thread`; `field(repr=False)` on any field that could hold a secret; max **25,000 chars** per code file (the four `shared/routing/` modules stay separate for this reason); no secret in stdout/stderr/logs.
- **Coverage:** 90% branch (`.coveragerc`, `fail_under=90`) — `shared` and `services/management/app` are already instrumented; `proxy` is instrumented for reporting. Builds fail below. The one genuinely live-only line (a real network bind, if any) carries `# pragma: no cover`; everything else is unit-tested with fakes/in-process stubs.
- **Focused test runs:** `.venv/bin/pytest <files> --no-cov -q` — `pytest.ini` injects `--cov` with `fail_under=90`, so any partial run exits non-zero on coverage even when all tests pass; read the test summary, not `$?`. The 90% gate runs once per wave via `make test-unit`.
- **Every commit message ends with these two trailer lines:**
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_013GYHFHJZgh6t5u1v3hTb14
  ```

---

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `services/management/alembic/versions/021_model_destinations.py` | Create | `owner_org_id` on `provider_credentials`; new `model_destinations` table |
| `services/management/app/models_sqlalchemy.py` | Modify | Add `owner_org_id` to `ProviderCredential`; add `ModelDestination` model + `MODEL_DESTINATION_*` status/const |
| `shared/auth/rbac.py` | Modify | Add `MODEL_DESTINATION_WRITE` / `MODEL_DESTINATION_DELETE` scopes + role bundles |
| `shared/utils/llm_connectors.py` | Modify | Bedrock config (region/endpoint/creds) + Bedrock exception-name mapping; Anthropic `base_url`; `is_retryable`/`classify_failure`; `_select_credential` pool exclusion (`owner_org_id IS NULL`) |
| `shared/routing/failover_gate.py` | Create | `FailoverGate` — flag + Enterprise entitlement, memoised 60 s, `evaluate(org)->(bool, reason)` |
| `shared/routing/destination_breaker.py` | Create | `DestinationBreaker` — per-`dest:{id}` `ProviderStats` state machine (threshold 3, cooldown 60 s), fed by the dispatcher |
| `shared/routing/destinations.py` | Create | `Destination`, `CredentialMaterial`, `DestinationResolver` (one `executesql` JOIN, TTL 30 s, pin/`local_only` filters) |
| `shared/routing/destination_connectors.py` | Create | `DestinationConnectorRegistry` — builds/reuses `LLMConnector` per credential version; ownership assert; LRU 256 / 15-min idle |
| `shared/routing/failover.py` | Create | `DestinationAttempt`, `Outcome`, `DestinationsExhausted`, `FailoverDispatcher.dispatch()` |
| `shared/utils/metrics.py` | Modify | `waddleai_destination_*` counters/gauges + record methods on `WaddleAIMetrics` |
| `proxy/apps/proxy_server/pipeline/stages.py` | Modify | `PipelineContext` new fields (`local_only`, `provider_pin`, `bytes_flushed`, `destination`); `RoutingStage` surfaces `clamp_local`/pin; `DispatchStage` failover branch |
| `shared/routing/engine.py` | Modify | `RouteDecision.clamp_local: bool`; `decide()` sets it from the sensitivity/budget clamp |
| `proxy/apps/proxy_server/main.py` | Modify | Merge `usage.waddleai.destination`; wire dispatcher/resolver/registry/gate in `startup()`; `destinations` in `/api/routing/stats` |
| `services/management/app/api/v1/routing_destinations.py` | Create | REST CRUD for destinations + BYOK credentials (quart-schema DTOs, gated, IDOR-safe) |
| `services/management/app/api/v1/__init__.py` | Modify | Append `routing_destinations,` to the route-module import tuple |
| `services/management/app/api/v1/providers.py` | Modify | Add `owner_org_id IS NULL` filter to the platform credential list/update/delete surface (S12) |
| `openapi/v1.yaml` | Modify (generated) | Regenerated via `make generate-openapi`, committed |
| `docs/docs-site/docs/architecture.md`, `docs/docs-site/docs/api/openai-compatible.md`, `docs/docs-site/docs/api/management-api.md`, `docs/docs-site/docs/routing/destination-failover.md` | Modify/Create | Failover box, pinning note, management-API rows, Bedrock→Anthropic walkthrough |
| `docs/RELEASE_NOTES.md` | Modify | Unreleased entry |
| `tests/unit/management/test_migration_021.py`, `test_rbac_destination_scopes.py`, `test_routing_destinations_routes.py`, `test_providers_credentials_tenant_filter.py` | Create | Migration, scopes, routes, S12 filter |
| `tests/unit/routing/test_failover_gate.py`, `test_destination_breaker.py`, `test_destination_resolver.py`, `test_destination_connectors.py`, `test_failover_dispatcher.py`, `test_routing_stage_failover.py`, `test_engine_clamp_local.py` | Create | Gate, breaker, resolver, registry, dispatcher, RoutingStage/engine |
| `tests/unit/test_llm_connectors.py` | Modify | Bedrock config/exception, Anthropic base_url, retryable classifier, pool exclusion (mutation-proven) |
| `tests/unit/proxy/test_dispatch_stage_failover.py` | Create | DispatchStage failover branch (fakes) + S10/S11 |
| `tests/unit/failover/test_two_stub_failover.py`, `tests/unit/failover/conftest.py`, `tests/unit/failover/__init__.py` | Create | In-process two-stub harness + scenarios |

**Key interfaces (defined ONCE here, referenced by every task):**

```python
# shared/routing/destinations.py
@dataclass(slots=True, frozen=True)
class Destination:
    """One resolved place to serve a logical model for one org. NEVER carries the secret."""
    id: int
    organization_id: int              # the tenant that owns this destination row
    model: str                        # logical model (post-routing)
    priority: int                     # 0 = active, >=1 = standby (ascending)
    provider_id: int
    provider_type: str                # openai|anthropic|bedrock|...
    endpoint_url: str | None
    region: str | None                # dest.region -> provider extra_config.region -> None
    provider_model_id: str | None     # None = same as `model`
    timeout_seconds: int              # resolved (NULL -> 30)
    credential_id: int | None         # None = platform pool / ambient
    owner_org_id: int | None          # credential owner; None = platform/ambient
    credential_version: str           # credential updated_at iso; part of the registry cache key

    @property
    def role(self) -> str:            # "active" | "standby"
        return "active" if self.priority == 0 else "standby"

@dataclass(slots=True, frozen=True)
class CredentialMaterial:
    """Secret-bearing credential row, loaded on demand by the registry only. Never cached/logged."""
    credential_id: int
    provider_id: int
    owner_org_id: int | None
    encrypted_material: str = field(repr=False, default="")   # 'enc:...' or plaintext; decrypt at build
    updated_at: str | None = None

class DestinationResolver:
    def __init__(self, db: Any, *, ttl_seconds: float = 30.0, clock: Callable[[], float] = time.monotonic) -> None: ...
    async def resolve(self, org_id: int, model: str, *, pin: str | None = None,
                      local_only: bool = False) -> list[Destination]: ...
    async def load_material(self, credential_id: int) -> CredentialMaterial | None: ...   # registry's credential_loader

# shared/routing/destination_connectors.py
class OwnershipError(Exception):
    """Registry build-time S2 assertion failed — the destination is skipped and logged."""

class DestinationConnectorRegistry:
    def __init__(self, credential_loader: Callable[[int], Awaitable[CredentialMaterial | None]],
                 *, max_size: int = 256, idle_seconds: float = 900.0,
                 clock: Callable[[], float] = time.monotonic) -> None: ...
    async def get(self, dest: Destination) -> LLMConnector: ...   # raises OwnershipError on S2 mismatch

# shared/routing/destination_breaker.py
class DestinationBreaker:
    def __init__(self, *, failure_threshold: int = 3, cooldown_seconds: float = 60.0,
                 clock: Callable[[], datetime] = datetime.utcnow) -> None: ...
    def is_open(self, dest_id: int) -> bool: ...
    def reserve_probe(self, dest_id: int) -> bool: ...     # True = this caller owns the single half-open probe
    def record_success(self, dest_id: int) -> None: ...
    def record_failure(self, dest_id: int) -> None: ...
    def snapshot(self) -> dict[str, dict[str, Any]]: ...   # for /api/routing/stats "destinations"

# shared/routing/failover.py
@dataclass(slots=True, frozen=True)
class DestinationAttempt:                                   # NOT AttemptRecord (that name is taken in llm_connectors.py)
    destination_id: int
    provider: str
    outcome: str                                            # "ok"|"failed"|"skipped"|"client_error"
    reason: str | None                                      # "breaker_open"|"rate_limit"|"timeout"|"server_error"|None

@dataclass(slots=True, frozen=True)
class Outcome:
    destination: Destination
    provider_type: str
    text: str
    usage: dict = field(repr=False, default_factory=dict)
    finish_reason: str = "stop"
    attempts: tuple[DestinationAttempt, ...] = ()

    @property
    def marker(self) -> dict: ...                           # usage.waddleai.destination payload (§5.7), secret-free

class DestinationsExhausted(Exception):
    def __init__(self, attempts: tuple[DestinationAttempt, ...], last_error: "ProviderError | None") -> None: ...
    def status_code(self) -> int: ...                       # 429 / 504 / 502 from the last retryable error
    def retry_after(self) -> float | None: ...               # last error's retry_after iff it's a ProviderRateLimitError

class FailoverDispatcher:
    def __init__(self, registry: DestinationConnectorRegistry, breaker: DestinationBreaker,
                 *, metrics: Any = None) -> None: ...
    async def dispatch(self, ctx: Any, destinations: list[Destination], messages: list) -> Outcome: ...

# shared/routing/failover_gate.py
class FailoverGate:
    def __init__(self, *, ttl_seconds: float = 60.0, license_getter: Callable[[], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None: ...
    async def evaluate(self, org_id: int) -> tuple[bool, str]: ...   # (enabled, "ok"|"flag_off"|"not_entitled")

# shared/utils/llm_connectors.py (added)
_RETRYABLE = (ProviderRateLimitError, ProviderTimeoutError, ProviderServerError)
def is_retryable(exc: BaseException) -> bool: ...
def classify_failure(exc: "ProviderError") -> str: ...      # "rate_limit"|"timeout"|"server_error"|"client_error"
```

---

## Execution Waves

Tasks in the same wave touch **disjoint file sets** and may run in parallel. Later waves consume earlier waves' interfaces (above), so within-session executors dispatch wave by wave. No two tasks in one wave edit the same file — in particular `llm_connectors.py`, `stages.py`, `rbac.py`, and `api/v1/__init__.py` each appear in at most one task per wave.

| Wave | Tasks | Depends on | Disjoint files? |
|---|---|---|---|
| 1 | T1 migration+models, T2 rbac scopes, T3 connector fixes+classifier, T4 breaker, T5 metrics, T6 gate | — | Yes — `alembic/021`+`models_sqlalchemy.py` / `rbac.py` / `llm_connectors.py` / `destination_breaker.py` / `metrics.py` / `failover_gate.py` all distinct |
| 2 | T7 pool-exclusion, T8 resolver, T9 registry, T10 dispatcher, T11 RoutingStage+engine, T12 management routes, T13 platform-credential filter | T1–T6 | Yes — `llm_connectors.py`(T7, sequenced after T3's wave) / `destinations.py` / `destination_connectors.py` / `failover.py` / `engine.py`+`stages.py` / `routing_destinations.py`+`api/v1/__init__.py` / `providers.py` all distinct |
| 3 | T14 DispatchStage integration + main marker + stats, T15 OpenAPI+docs+release notes, T16 in-process two-stub harness | T7–T13 | Yes — `stages.py`+`main.py` / `openapi/v1.yaml`+docs / `tests/unit/failover/` all distinct (`stages.py` edited by T11 in W2, T14 in W3 — different waves) |

> **Note on T7 vs T3:** both edit `shared/utils/llm_connectors.py`, so they are in different waves (T3 in Wave 1, T7 in Wave 2). T7 also depends on T1 (the `owner_org_id` column must exist for the mirror/reflection to resolve).

---

### Task 1: Migration 021 + models — `owner_org_id` and `model_destinations` (security-critical: S2/S3 schema)

**Files:**
- Create: `services/management/alembic/versions/021_model_destinations.py`
- Modify: `services/management/app/models_sqlalchemy.py` (add `owner_org_id` + `updated_at` to `ProviderCredential`; add `ModelDestination`)
- Test: `tests/unit/management/test_migration_021.py`

**Interfaces:**
- Produces: `provider_credentials.owner_org_id INTEGER NULL` FK `organizations.id` ON DELETE CASCADE (indexed); `provider_credentials.updated_at DATETIME NULL` (server-defaulted, bumped on update); table `model_destinations` (columns per spec §3.2). `provider_credentials.updated_at` is what Task 8's `DestinationResolver.load_material` reads as `CredentialMaterial.updated_at` / `Destination.credential_version`. Consumed by Tasks 7 (pool exclusion), 8 (resolver), 12 (routes), 13 (S12 filter).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/management/test_migration_021.py
"""Migration 021: owner_org_id + updated_at on provider_credentials, model_destinations.

Import mechanism mirrors ``test_migration_020.py`` -- the module is loaded by path
(``importlib.util.spec_from_file_location``), never a dotted ``__import__`` of the
versions package (filenames starting with a digit aren't valid dotted identifiers).
``owner_org_id``/``updated_at`` existence is asserted on the SQLAlchemy model (the
schema authority per this migration's own docstring); a live upgrade()/downgrade()
round-trip against a scratch SQLite DB is not used here because upgrade() adds the
``owner_org_id`` FK via ``op.create_foreign_key`` outside batch mode, which SQLite's
ALTER-table support does not implement (see ``test_migration_019.py`` for the batch-mode
alternative used when a migration's downgrade path needs it).
"""
from __future__ import annotations

import importlib.util
import os

from app.models_sqlalchemy import ModelDestination, ProviderCredential

MIGRATION_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "..",
    "..",
    "services",
    "management",
    "alembic",
    "versions",
    "021_model_destinations.py",
)


def _load_migration_021():
    """Import ``021_model_destinations.py`` by path (filename isn't an identifier)."""
    spec = importlib.util.spec_from_file_location("migration_021_model_destinations", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_chain_and_callables():
    mod = _load_migration_021()
    assert mod.revision == "021_model_destinations"
    assert mod.down_revision == "020_graph_instances"
    assert callable(mod.upgrade) and callable(mod.downgrade)


def test_provider_credential_gains_owner_org_id_and_updated_at():
    cols = {c.name for c in ProviderCredential.__table__.columns}
    assert "owner_org_id" in cols
    assert "updated_at" in cols
    # The pre-existing provider-workspace column is untouched and is a different type.
    assert "org_id" in cols
    owner = ProviderCredential.__table__.columns["owner_org_id"]
    assert owner.nullable is True
    assert str(owner.type).upper().startswith("INTEGER")
    updated = ProviderCredential.__table__.columns["updated_at"]
    assert updated.nullable is True
    assert str(updated.type).upper().startswith("DATETIME")


def test_model_destinations_shape():
    cols = {c.name for c in ModelDestination.__table__.columns}
    assert {
        "id", "organization_id", "model", "priority", "provider_id", "credential_id",
        "provider_model_id", "region", "timeout_seconds", "enabled", "created_at", "updated_at",
    } <= cols
    assert ModelDestination.__tablename__ == "model_destinations"
    uniques = {
        tuple(sorted(c.name for c in con.columns))
        for con in ModelDestination.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    assert ("model", "organization_id", "priority") in uniques
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/management/test_migration_021.py -v --no-cov`
Expected: FAIL — `ModelDestination` / migration module not found.

- [ ] **Step 3: Write minimal implementation**

`services/management/alembic/versions/021_model_destinations.py` (mirror 020 exactly; enum-like columns are plain strings + app validation per house style):
```python
"""owner_org_id on provider_credentials + model_destinations (spec §3).

``provider_credentials.owner_org_id`` (NULL = platform pool, unchanged; non-null =
tenant-owned/BYOK). ``model_destinations`` is the ordered active/standby destination
list for one (org, logical model). Enum-like columns are plain strings validated in
the app (house style, mig 018). SQLAlchemy models are the schema authority; the proxy
reads these via parameterized executesql (spec §5.2).

Revision ID: 021_model_destinations
Revises: 020_graph_instances
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "021_model_destinations"
down_revision: str | None = "020_graph_instances"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add owner_org_id + updated_at to provider_credentials; create model_destinations."""
    op.add_column(
        "provider_credentials",
        sa.Column("owner_org_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "provider_credentials",
        sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
    )
    op.create_foreign_key(
        "fk_provider_credentials_owner_org",
        "provider_credentials", "organizations",
        ["owner_org_id"], ["id"], ondelete="CASCADE",
    )
    op.create_index(
        "ix_provider_credentials_owner_org_id", "provider_credentials", ["owner_org_id"]
    )
    op.create_table(
        "model_destinations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("credential_id", sa.Integer(), nullable=True),
        sa.Column("provider_model_id", sa.String(255), nullable=True),
        sa.Column("region", sa.String(64), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["provider_id"], ["ai_providers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["credential_id"], ["provider_credentials.id"], ondelete="SET NULL"
        ),
        sa.CheckConstraint("priority >= 0", name="ck_model_destinations_priority"),
        sa.CheckConstraint(
            "timeout_seconds IS NULL OR (timeout_seconds >= 1 AND timeout_seconds <= 600)",
            name="ck_model_destinations_timeout",
        ),
        sa.UniqueConstraint(
            "organization_id", "model", "priority", name="uq_model_destinations_org_model_priority"
        ),
    )
    op.create_index(
        "ix_model_destinations_org_model", "model_destinations", ["organization_id", "model"]
    )


def downgrade() -> None:
    """Drop model_destinations and provider_credentials.owner_org_id/updated_at."""
    op.drop_index("ix_model_destinations_org_model", "model_destinations")
    op.drop_table("model_destinations")
    op.drop_index("ix_provider_credentials_owner_org_id", "provider_credentials")
    op.drop_constraint(
        "fk_provider_credentials_owner_org", "provider_credentials", type_="foreignkey"
    )
    op.drop_column("provider_credentials", "owner_org_id")
    op.drop_column("provider_credentials", "updated_at")
```

In `services/management/app/models_sqlalchemy.py` add `owner_org_id` + `updated_at` to `ProviderCredential` (next to the existing `org_id`/`created_at`, keeping the sharp distinction documented). The file imports individual SQLAlchemy names (no `import sqlalchemy as sa`) and already has `from datetime import datetime` — `func` is not yet imported, so add it to the existing `from sqlalchemy import (...)` block (see the import note below) and reference it unqualified, not as `sa.func`:
```python
    # Tenant owner (BYOK). NULL = platform pool (existing behaviour, unchanged);
    # non-null = usable ONLY by that org's destinations, never the platform pool.
    # NOTE: distinct from `org_id` above (the provider's own workspace id, a String).
    owner_org_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Bumped on every row update; the registry's cache key (`credential_version`,
    # Tasks 8/9) so a rotated credential yields a fresh connector, not a stale one.
    updated_at = Column(DateTime, nullable=True, server_default=func.now(), onupdate=datetime.utcnow)
```
and add the new model near `AIProvider`:
```python
class ModelDestination(Base):
    """One active/standby destination for a logical model, per org (spec §3.2).

    priority 0 = active, >=1 = standby (tried ascending). Failover is implicit
    when >=2 enabled rows exist for one (org, model); at most 5 enabled per pair
    (API-enforced). ``credential_id`` NULL = the provider's platform pool / ambient.
    """

    __tablename__ = "model_destinations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model = Column(String(255), nullable=False)
    priority = Column(Integer, nullable=False)
    provider_id = Column(
        Integer, ForeignKey("ai_providers.id", ondelete="RESTRICT"), nullable=False
    )
    credential_id = Column(
        Integer, ForeignKey("provider_credentials.id", ondelete="SET NULL"), nullable=True
    )
    provider_model_id = Column(String(255), nullable=True)
    region = Column(String(64), nullable=True)
    timeout_seconds = Column(Integer, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "model", "priority", name="uq_model_destinations_org_model_priority"
        ),
        CheckConstraint("priority >= 0", name="ck_model_destinations_priority"),
        CheckConstraint(
            "timeout_seconds IS NULL OR (timeout_seconds >= 1 AND timeout_seconds <= 600)",
            name="ck_model_destinations_timeout",
        ),
    )
```
Also add the API cap constant near the model: `MAX_DESTINATIONS_PER_MODEL = 5`. Ensure `CheckConstraint`, `UniqueConstraint`, `text`, `func` are imported at the top of `models_sqlalchemy.py` (add any that are missing to the existing SQLAlchemy import line).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/management/test_migration_021.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/management/alembic/versions/021_model_destinations.py services/management/app/models_sqlalchemy.py tests/unit/management/test_migration_021.py
git commit -m "feat(failover): model_destinations table + provider_credentials.owner_org_id" \
  -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" \
  -m "Claude-Session: https://claude.ai/code/session_013GYHFHJZgh6t5u1v3hTb14"
```

---

### Task 2: RBAC destination scopes (security-critical: S1)

**Files:**
- Modify: `shared/auth/rbac.py` (add scopes + role bundles)
- Modify: `tests/unit/management/test_scope_authz.py` (keep its fixed scope-tier lists exhaustive)
- Test: `tests/unit/management/test_rbac_destination_scopes.py`

**Interfaces:**
- Produces: `Permission.MODEL_DESTINATION_WRITE` (`model_destination:write`, admin + resource_manager) and `Permission.MODEL_DESTINATION_DELETE` (`model_destination:delete`, admin only). Consumed by Task 12 route decorators.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/management/test_rbac_destination_scopes.py
from __future__ import annotations
from shared.auth.rbac import Permission, ROLE_PERMISSIONS, Role


def test_scopes_exist_with_expected_values():
    assert Permission.MODEL_DESTINATION_WRITE.value == "model_destination:write"
    assert Permission.MODEL_DESTINATION_DELETE.value == "model_destination:delete"


def test_write_is_admin_and_resource_manager():
    assert Permission.MODEL_DESTINATION_WRITE in ROLE_PERMISSIONS[Role.ADMIN]
    assert Permission.MODEL_DESTINATION_WRITE in ROLE_PERMISSIONS[Role.RESOURCE_MANAGER]


def test_delete_is_admin_only():
    assert Permission.MODEL_DESTINATION_DELETE in ROLE_PERMISSIONS[Role.ADMIN]
    assert Permission.MODEL_DESTINATION_DELETE not in ROLE_PERMISSIONS[Role.RESOURCE_MANAGER]
    assert Permission.MODEL_DESTINATION_DELETE not in ROLE_PERMISSIONS[Role.REPORTER]
    assert Permission.MODEL_DESTINATION_DELETE not in ROLE_PERMISSIONS[Role.USER]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/management/test_rbac_destination_scopes.py -v --no-cov`
Expected: FAIL — `MODEL_DESTINATION_WRITE` not defined.

- [ ] **Step 3: Write minimal implementation**

In `shared/auth/rbac.py`, in the `Permission` enum next to `MODEL_ACCESS_POLICY_*`:
```python
    # Model destination CRUD (provider-destination-failover spec §4).
    MODEL_DESTINATION_WRITE = "model_destination:write"  # admin + resource_manager
    MODEL_DESTINATION_DELETE = "model_destination:delete"  # admin only
```
In `ROLE_PERMISSIONS`, add both to `Role.ADMIN` (next to `Permission.MODEL_ACCESS_POLICY_DELETE`), and add `Permission.MODEL_DESTINATION_WRITE` to `Role.RESOURCE_MANAGER` (next to `Permission.MODEL_ACCESS_POLICY_WRITE`).

> If `tests/unit/management/test_scope_authz.py` enumerates scope tiers from a fixed list, add `MODEL_DESTINATION_WRITE` to its resource-manager tier set and `MODEL_DESTINATION_DELETE` to its admin-only tier set so that guard stays exhaustive. Run `.venv/bin/pytest tests/unit/management/test_scope_authz.py -v` and fix any tier-membership assertion it raises.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/management/test_rbac_destination_scopes.py tests/unit/management/test_scope_authz.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/auth/rbac.py tests/unit/management/test_scope_authz.py tests/unit/management/test_rbac_destination_scopes.py
git commit -m "feat(failover): MODEL_DESTINATION_WRITE/DELETE scopes + role bundles" \
  -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" \
  -m "Claude-Session: https://claude.ai/code/session_013GYHFHJZgh6t5u1v3hTb14"
```

---

### Task 3: Connector fixes + retryable classifier (security-critical: S4/S5)

**Files:**
- Modify: `shared/utils/llm_connectors.py` (Bedrock `_get_client` config; Bedrock exception-name mapping; Anthropic `base_url`; module-level `is_retryable`/`classify_failure`; `ProviderRateLimitError.retry_after`)
- Test: `tests/unit/test_llm_connectors.py` (add cases)

**Interfaces:**
- Produces: `is_retryable(exc) -> bool` and `classify_failure(exc) -> str` (module-level, consumed by Tasks 10, 16); a `BedrockConnector` that reads `aws_region`/`endpoint_url`/AWS keys from `config` and maps Bedrock error codes; an `AnthropicConnector` that honours `base_url=endpoint_url`; `ProviderRateLimitError.retry_after: float | None = None` (optional keyword arg, backward-compatible), populated from the upstream `Retry-After` header on OpenAI/Anthropic SDK 429s and from Bedrock's `ThrottlingException` response metadata. Consumed by Task 9 (registry builds these connectors with per-destination config) and Task 10 (`DestinationsExhausted.retry_after()` reads it off the last error).

**Rule reminders:** the classifier is the single source of truth for the retryable/non-retryable split (Global Constraints taxonomy). Bedrock material is a JSON object `{"aws_access_key_id","aws_secret_access_key","aws_session_token"?}`; an empty material means "ambient AWS chain". Secrets stay inside the connector — never log `config`.

- [ ] **Step 1: Write the failing test** (append to `tests/unit/test_llm_connectors.py`)

```python
# --- append to tests/unit/test_llm_connectors.py ---
from __future__ import annotations
import json
from shared.utils.llm_connectors import (
    AnthropicConnector, BedrockConnector, ProviderClientError, ProviderRateLimitError,
    ProviderServerError, ProviderTimeoutError, classify_failure, is_retryable,
)


def test_is_retryable_matrix():
    assert is_retryable(ProviderRateLimitError("p", "m", "x")) is True
    assert is_retryable(ProviderTimeoutError("p", "m", "x")) is True
    assert is_retryable(ProviderServerError("p", "m", "x")) is True
    assert is_retryable(ProviderClientError("p", "m", "x", status_code=401)) is False
    assert is_retryable(ValueError("nope")) is False


def test_classify_failure_labels():
    assert classify_failure(ProviderRateLimitError("p", "m", "x")) == "rate_limit"
    assert classify_failure(ProviderTimeoutError("p", "m", "x")) == "timeout"
    assert classify_failure(ProviderServerError("p", "m", "x")) == "server_error"
    assert classify_failure(ProviderClientError("p", "m", "x", status_code=403)) == "client_error"


def test_anthropic_uses_endpoint_url_as_base_url(monkeypatch):
    seen = {}

    class _FakeAsyncAnthropic:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    import anthropic
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeAsyncAnthropic)
    AnthropicConnector("a", {"api_key": "sk-x", "endpoint_url": "https://vpc.example/anthropic"})
    assert seen["base_url"] == "https://vpc.example/anthropic"
    # default host when endpoint_url is absent -> base_url not forced
    seen.clear()
    AnthropicConnector("a", {"api_key": "sk-x"})
    assert "base_url" not in seen or seen["base_url"] is None


def test_bedrock_reads_region_and_credentials_from_config(monkeypatch):
    captured = {}

    def _fake_client(service, **kwargs):
        captured["service"] = service
        captured.update(kwargs)
        return object()

    import shared.utils.llm_connectors as mod
    fake_boto3 = type("B", (), {"client": staticmethod(_fake_client)})
    monkeypatch.setattr(mod, "boto3", fake_boto3)
    material = json.dumps({"aws_access_key_id": "AKIA", "aws_secret_access_key": "sec"})
    conn = BedrockConnector("b", {"api_key": material, "aws_region": "eu-west-1"})
    import asyncio
    asyncio.get_event_loop().run_until_complete(conn._get_client())
    assert captured["service"] == "bedrock-runtime"
    assert captured["region_name"] == "eu-west-1"
    assert captured["aws_access_key_id"] == "AKIA" and captured["aws_secret_access_key"] == "sec"


def test_bedrock_ambient_chain_when_material_empty(monkeypatch):
    captured = {}

    def _fake_client(service, **kwargs):
        captured.update(kwargs); captured["service"] = service; return object()

    import shared.utils.llm_connectors as mod
    monkeypatch.setattr(mod, "boto3", type("B", (), {"client": staticmethod(_fake_client)}))
    import asyncio
    conn = BedrockConnector("b", {"api_key": "", "aws_region": "us-east-2"})
    asyncio.get_event_loop().run_until_complete(conn._get_client())
    assert captured["region_name"] == "us-east-2"
    assert "aws_access_key_id" not in captured   # ambient chain, no static keys passed


def test_provider_rate_limit_error_retry_after_defaults_none():
    assert ProviderRateLimitError("p", "m", "x").retry_after is None


def test_provider_rate_limit_error_retry_after_can_be_set():
    assert ProviderRateLimitError("p", "m", "x", retry_after=7.0).retry_after == 7.0


def test_retry_after_extracted_from_sdk_exception_headers():
    from shared.utils.llm_connectors import _retry_after_from_headers

    class _Resp:
        headers = {"retry-after": "7"}

    class _FakeSDKError(Exception):
        response = _Resp()

    assert _retry_after_from_headers(_FakeSDKError("rate limited")) == 7.0


def test_retry_after_extraction_ignores_parse_errors_and_missing_header():
    from shared.utils.llm_connectors import _retry_after_from_headers

    class _RespBad:
        headers = {"retry-after": "not-a-number"}

    class _FakeSDKErrorBad(Exception):
        response = _RespBad()

    assert _retry_after_from_headers(_FakeSDKErrorBad("x")) is None
    assert _retry_after_from_headers(ValueError("no response attr")) is None


def test_bedrock_throttling_maps_retry_after_from_response_metadata(monkeypatch):
    from botocore.exceptions import ClientError

    class _FakeClient:
        def converse(self, **kwargs):
            raise ClientError(
                {
                    "Error": {"Code": "ThrottlingException", "Message": "slow down"},
                    "RetryAfterSeconds": "3",
                },
                "Converse",
            )

    conn = BedrockConnector("b", {"api_key": "", "aws_region": "us-east-2"})

    async def _fake_get_client():
        return _FakeClient()

    monkeypatch.setattr(conn, "_get_client", _fake_get_client)
    import asyncio

    with pytest.raises(ProviderRateLimitError) as ei:
        asyncio.get_event_loop().run_until_complete(
            conn.chat_completion([{"role": "user", "content": "hi"}], model="m")
        )
    assert ei.value.retry_after == 3.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_llm_connectors.py -k "retryable or classify or anthropic_uses or bedrock_reads or bedrock_ambient or retry_after or bedrock_throttling" -v --no-cov`
Expected: FAIL — `classify_failure`/`is_retryable` not defined; Bedrock ignores config; Anthropic ignores `endpoint_url`; `ProviderRateLimitError` has no `retry_after`; `_retry_after_from_headers` not defined.

- [ ] **Step 3: Write minimal implementation**

Add module-level helpers (near the `ProviderError` classes; `_RETRYABLE` reuses the exact tuple `DispatchStage` already catches):
```python
_RETRYABLE = (ProviderRateLimitError, ProviderTimeoutError, ProviderServerError)


def is_retryable(exc: BaseException) -> bool:
    """True iff ``exc`` is a retryable provider failure (timeout/429/5xx); the single split source."""
    return isinstance(exc, _RETRYABLE)


def classify_failure(exc: "ProviderError") -> str:
    """Map a ProviderError to a stable reason label for metrics/attempt records."""
    if isinstance(exc, ProviderRateLimitError):
        return "rate_limit"
    if isinstance(exc, ProviderTimeoutError):
        return "timeout"
    if isinstance(exc, ProviderServerError):
        return "server_error"
    return "client_error"


def _retry_after_from_headers(exc: BaseException) -> float | None:
    """Best-effort Retry-After (seconds) from an SDK exception's HTTP response headers."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if headers is None:
        return None
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
```
Give `ProviderRateLimitError` an optional, backward-compatible `retry_after` attribute (existing call sites — positional or keyword, with or without `status_code` — are unaffected since it's appended last with a default):
```python
class ProviderRateLimitError(ProviderError):
    """Rate limit error HTTP 429 (retryable)."""

    def __init__(
        self,
        provider: str,
        model: str,
        message: str,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Bind the standard ProviderError fields plus an optional Retry-After seconds hint."""
        super().__init__(provider, model, message, status_code=status_code)
        self.retry_after = retry_after
```
In `OpenAIConnector.chat_completion`/`stream_chat_completion` and `AnthropicConnector.chat_completion`/`stream_chat_completion`'s `except openai.RateLimitError`/`except anthropic.RateLimitError` 429 handling, pass `retry_after=_retry_after_from_headers(e)` (each site already raises `ProviderRateLimitError(..., status_code=429)`; add the new kwarg, e.g.):
```python
        except openai.RateLimitError as e:
            raise ProviderRateLimitError(
                provider="openai",
                model=model,
                message="OpenAI rate limit",
                status_code=429,
                retry_after=_retry_after_from_headers(e),
            ) from e
```
(same pattern for the Anthropic `except anthropic.RateLimitError as e:` sites — `provider="anthropic"`, message text unchanged, just add `retry_after=_retry_after_from_headers(e)`.)

In `AnthropicConnector.__init__`, pass `base_url` when `endpoint_url` is set:
```python
        if self.endpoint_url:
            self.client = anthropic.AsyncAnthropic(api_key=self.api_key, base_url=self.endpoint_url)
        else:
            self.client = anthropic.AsyncAnthropic(api_key=self.api_key)
```
In `BedrockConnector.__init__`, parse region + credential material once (store secrets on the instance only; never log):
```python
        import json as _json
        self.aws_region = config.get("aws_region") or config.get("region") or "us-east-1"
        self._aws_creds: dict[str, str] = {}
        material = config.get("api_key") or ""
        if material:
            try:
                parsed = _json.loads(material)
                for k in ("aws_access_key_id", "aws_secret_access_key", "aws_session_token"):
                    if parsed.get(k):
                        self._aws_creds[k] = parsed[k]
            except (ValueError, TypeError):
                logger.warning("BedrockConnector %s: credential material is not valid JSON", name)
```
Rewrite `_get_client`'s `_create_client` to use them (fall back to ambient chain when `_aws_creds` is empty; honour `endpoint_url` for VPC endpoints):
```python
            def _create_client():
                kwargs = {"region_name": self.aws_region}
                if self.endpoint_url:
                    kwargs["endpoint_url"] = self.endpoint_url
                kwargs.update(self._aws_creds)
                return boto3.client("bedrock-runtime", **kwargs)
```
Add a Bedrock retry-after helper next to `_retry_after_from_headers` (boto3's `ClientError.response` carries a top-level `RetryAfterSeconds` on some throttling responses, else it's in the response headers):
```python
def _bedrock_retry_after(exc: "ClientError") -> float | None:
    """Best-effort Retry-After (seconds) from a Bedrock ClientError's response metadata."""
    raw = exc.response.get("RetryAfterSeconds")
    if raw is None:
        raw = exc.response.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
```
Add Bedrock error-code-name mapping in the `chat_completion`/`stream_chat_completion` `except ClientError` block (before the HTTP-status fallback already present), mapping by `e.response["Error"]["Code"]`; populate `retry_after` on the `ThrottlingException`/`ModelNotReadyException` branch:
```python
                        code = e.response.get("Error", {}).get("Code", "")
                        if code in ("ThrottlingException", "ModelNotReadyException"):
                            raise ProviderRateLimitError(
                                provider="bedrock", model=model, message=code, status_code=429,
                                retry_after=_bedrock_retry_after(e),
                            ) from e
                        if code in ("ServiceUnavailableException", "InternalServerException"):
                            raise ProviderServerError(
                                provider="bedrock", model=model, message=code, status_code=503
                            ) from e
```
(Keep the existing HTTP-status-based mapping as the fallback for codes not named above.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_llm_connectors.py -v --no-cov`
Expected: PASS (new cases plus the existing connector suite).

- [ ] **Step 5: Commit**

```bash
git add shared/utils/llm_connectors.py tests/unit/test_llm_connectors.py
git commit -m "feat(failover): Bedrock config+error mapping, Anthropic base_url, retryable classifier" \
  -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" \
  -m "Claude-Session: https://claude.ai/code/session_013GYHFHJZgh6t5u1v3hTb14"
```

---

### Task 4: `DestinationBreaker` (per-destination circuit breaker)

**Files:**
- Create: `shared/routing/destination_breaker.py`
- Test: `tests/unit/routing/test_destination_breaker.py`

**Interfaces:**
- Consumes: `ProviderStats` from `shared.utils.request_router` (reuse the state fields).
- Produces: `DestinationBreaker` (signatures in Key Interfaces). Keyed `dest:{id}`, `failure_threshold=3`, `cooldown_seconds=60`, in-process. Consumed by Task 10 (dispatcher) and Task 14 (`/api/routing/stats`).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/routing/test_destination_breaker.py
from __future__ import annotations
from datetime import datetime, timedelta
from shared.routing.destination_breaker import DestinationBreaker


class _Clock:
    def __init__(self): self.now = datetime(2026, 9, 4, 12, 0, 0)
    def __call__(self): return self.now
    def advance(self, seconds): self.now += timedelta(seconds=seconds)


def test_closed_until_threshold():
    b = DestinationBreaker(failure_threshold=3, cooldown_seconds=60)
    assert b.is_open(1) is False
    b.record_failure(1); b.record_failure(1)
    assert b.is_open(1) is False          # 2 < 3
    b.record_failure(1)
    assert b.is_open(1) is True           # tripped


def test_cooldown_then_single_half_open_probe():
    clock = _Clock()
    b = DestinationBreaker(failure_threshold=3, cooldown_seconds=60, clock=clock)
    for _ in range(3):
        b.record_failure(7)
    assert b.is_open(7) is True
    assert b.reserve_probe(7) is False        # still cooling down
    clock.advance(61)
    assert b.reserve_probe(7) is True         # first caller gets the probe
    assert b.reserve_probe(7) is False        # second caller refused (single probe)


def test_success_closes_breaker():
    b = DestinationBreaker(failure_threshold=3, cooldown_seconds=60)
    for _ in range(3):
        b.record_failure(2)
    b.record_success(2)
    assert b.is_open(2) is False
    assert b.reserve_probe(2) is True         # closed -> probe trivially available


def test_snapshot_reports_state():
    b = DestinationBreaker()
    b.record_failure(5)
    snap = b.snapshot()
    assert "dest:5" in snap
    assert snap["dest:5"]["consecutive_failures"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/routing/test_destination_breaker.py -v --no-cov`
Expected: FAIL — module not defined.

- [ ] **Step 3: Write minimal implementation**

`shared/routing/destination_breaker.py`:
```python
"""Per-destination circuit breaker (spec §5.6).

Reuses request_router.ProviderStats's closed->open->half-open state shape, keyed
``dest:{id}``, with its OWN parameters (failure_threshold=3, cooldown=60s) and fed
by FailoverDispatcher on every attempt — the first live consumer of the breaker.
In-process per replica; the Valkey-shared breaker (platform-spec §5.3.4) is a
documented follow-up, and each replica still fails over correctly on its own evidence.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from shared.utils.request_router import ProviderStats


class DestinationBreaker:
    """Closed/open/half-open breaker per destination id, fed by the dispatcher."""

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
        clock: Callable[[], datetime] = datetime.utcnow,
    ) -> None:
        """Configure thresholds and bind an injectable clock (for deterministic tests)."""
        self._threshold = failure_threshold
        self._cooldown = timedelta(seconds=cooldown_seconds)
        self._clock = clock
        self._stats: dict[int, ProviderStats] = {}

    def _s(self, dest_id: int) -> ProviderStats:
        return self._stats.setdefault(dest_id, ProviderStats())

    def _in_cooldown(self, s: ProviderStats) -> bool:
        return (
            s.last_failure is not None
            and (not s.last_success or s.last_failure > s.last_success)
            and (self._clock() - s.last_failure) < self._cooldown
        )

    def is_open(self, dest_id: int) -> bool:
        """True while the destination is tripped and inside its cooldown window."""
        s = self._s(dest_id)
        if s.consecutive_failures < self._threshold:
            return False
        return self._in_cooldown(s)

    def reserve_probe(self, dest_id: int) -> bool:
        """Reserve the single half-open probe once cooldown has elapsed; False if unavailable."""
        s = self._s(dest_id)
        if s.consecutive_failures < self._threshold:
            return True                          # closed -> not gated
        if self._in_cooldown(s):
            return False                         # open
        if s.half_open_probe_in_flight:
            return False                         # probe already taken
        s.half_open_probe_in_flight = True
        return True

    def record_success(self, dest_id: int) -> None:
        """Reset the breaker for this destination after a successful attempt."""
        s = self._s(dest_id)
        s.consecutive_failures = 0
        s.last_success = self._clock()
        s.half_open_probe_in_flight = False

    def record_failure(self, dest_id: int) -> None:
        """Record a retryable failure; trips the breaker at the threshold."""
        s = self._s(dest_id)
        s.consecutive_failures += 1
        s.last_failure = self._clock()
        s.half_open_probe_in_flight = False

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Serialisable breaker state for /api/routing/stats (no secrets)."""
        return {
            f"dest:{dest_id}": {
                "consecutive_failures": s.consecutive_failures,
                "open": self.is_open(dest_id),
                "last_failure": s.last_failure.isoformat() if s.last_failure else None,
                "last_success": s.last_success.isoformat() if s.last_success else None,
            }
            for dest_id, s in self._stats.items()
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/routing/test_destination_breaker.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/routing/destination_breaker.py tests/unit/routing/test_destination_breaker.py
git commit -m "feat(failover): per-destination circuit breaker" \
  -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" \
  -m "Claude-Session: https://claude.ai/code/session_013GYHFHJZgh6t5u1v3hTb14"
```

---

### Task 5: Destination Prometheus metrics

**Files:**
- Modify: `shared/utils/metrics.py` (add collectors + record methods to `WaddleAIMetrics`)
- Test: `tests/unit/test_metrics_destinations.py`

**Interfaces:**
- Produces (on `WaddleAIMetrics`): `record_destination_attempt(provider_type, outcome)`, `record_destination_failover(from_provider, to_provider, reason)`, `set_destination_breaker_open(destination_id, is_open)`, `record_destination_gate_denied(reason)`. Backing collectors: `waddleai_destination_attempts_total{provider_type,outcome}`, `waddleai_destination_failover_total{from_provider,to_provider,reason}`, `waddleai_destination_breaker_open{destination_id}` (gauge), `waddleai_destination_gate_denied_total{reason}`. Consumed by Tasks 10, 14.

**Rule reminder:** `WaddleAIMetrics` is Borg — collectors are built once on first instantiation and stored in `_shared_collectors`. Add the four new collectors to **both** the first-build block and the `_shared_collectors` dict so a second instance rebinds them.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_metrics_destinations.py
from __future__ import annotations
from shared.utils.metrics import WaddleAIMetrics


def _value(metric, **labels):
    return metric.labels(**labels)._value.get()


def test_attempt_and_failover_and_gate_counters():
    m = WaddleAIMetrics("test-proxy")
    m.record_destination_attempt("openai", "ok")
    m.record_destination_attempt("openai", "failed")
    m.record_destination_failover("openai", "anthropic", "server_error")
    m.record_destination_gate_denied("flag_off")
    assert _value(m.destination_attempts_total, provider_type="openai", outcome="ok") == 1.0
    assert _value(
        m.destination_failover_total, from_provider="openai", to_provider="anthropic",
        reason="server_error",
    ) == 1.0
    assert _value(m.destination_gate_denied_total, reason="flag_off") == 1.0


def test_breaker_gauge_set():
    m = WaddleAIMetrics("test-proxy")
    m.set_destination_breaker_open("42", True)
    assert _value(m.destination_breaker_open, destination_id="42") == 1.0
    m.set_destination_breaker_open("42", False)
    assert _value(m.destination_breaker_open, destination_id="42") == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_metrics_destinations.py -v --no-cov`
Expected: FAIL — collectors/methods not defined.

- [ ] **Step 3: Write minimal implementation**

In the first-build block of `WaddleAIMetrics.__init__` (near `provider_health`):
```python
        # Provider-destination failover metrics (failover spec §5.7)
        self.destination_attempts_total = Counter(
            "waddleai_destination_attempts_total",
            "Destination attempts by provider type and outcome",
            ["provider_type", "outcome"],
        )
        self.destination_failover_total = Counter(
            "waddleai_destination_failover_total",
            "Failovers from one destination to the next",
            ["from_provider", "to_provider", "reason"],
        )
        self.destination_breaker_open = Gauge(
            "waddleai_destination_breaker_open",
            "Destination breaker state (1=open, 0=closed)",
            ["destination_id"],
        )
        self.destination_gate_denied_total = Counter(
            "waddleai_destination_gate_denied_total",
            "Requests that did not use destination failover due to the gate",
            ["reason"],
        )
```
Add the four names to the `_shared_collectors` dict that the Borg path rebinds (wherever `_shared_collectors` is populated after the first build — add the four attributes alongside the existing collectors). Then add the record methods:
```python
    def record_destination_attempt(self, provider_type: str, outcome: str) -> None:
        """Count one destination attempt (outcome: ok|failed|skipped|client_error)."""
        self.destination_attempts_total.labels(provider_type=provider_type, outcome=outcome).inc()

    def record_destination_failover(self, from_provider: str, to_provider: str, reason: str) -> None:
        """Count one failover hop from one destination to the next."""
        self.destination_failover_total.labels(
            from_provider=from_provider, to_provider=to_provider, reason=reason
        ).inc()

    def set_destination_breaker_open(self, destination_id: str, is_open: bool) -> None:
        """Publish a destination breaker's open/closed state."""
        self.destination_breaker_open.labels(destination_id=destination_id).set(1 if is_open else 0)

    def record_destination_gate_denied(self, reason: str) -> None:
        """Count a request that fell back to the existing path (reason: flag_off|not_entitled)."""
        self.destination_gate_denied_total.labels(reason=reason).inc()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_metrics_destinations.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/utils/metrics.py tests/unit/test_metrics_destinations.py
git commit -m "feat(failover): destination attempt/failover/breaker/gate metrics" \
  -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" \
  -m "Claude-Session: https://claude.ai/code/session_013GYHFHJZgh6t5u1v3hTb14"
```

---

### Task 6: `FailoverGate` (flag + entitlement, memoised) (security-critical: S10)

**Files:**
- Create: `shared/routing/failover_gate.py`
- Test: `tests/unit/routing/test_failover_gate.py`

**Interfaces:**
- Consumes: `is_feature_enabled` (existing), `penguin_licensing.LicenseClient` (existing pattern, `model_access_policies.py`).
- Produces: `FailoverGate.evaluate(org_id) -> (enabled: bool, reason: str)` where reason ∈ `"ok"|"flag_off"|"not_entitled"`. Flag `waddleai.provider_failover` (default OFF, fail-safe) AND entitlement `waddleai_provider_failover` (fail-closed); result memoised per org for `ttl_seconds` (default 60). Consumed by Task 14 (`DispatchStage`) and Task 12 (management gate reuses the same flag/entitlement names, its own 404/403 shape).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/routing/test_failover_gate.py
from __future__ import annotations
from unittest.mock import MagicMock
import pytest
from shared.routing.failover_gate import FailoverGate


class _Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def tick(self, dt): self.t += dt


def _lic(entitled: bool):
    client = MagicMock()
    client.check_feature.return_value = entitled
    return lambda: client


@pytest.mark.asyncio
async def test_flag_off_denies(monkeypatch):
    monkeypatch.setenv("WADDLEAI_FLAG_PROVIDER_FAILOVER", "0")
    gate = FailoverGate(license_getter=_lic(True))
    assert await gate.evaluate(7) == (False, "flag_off")


@pytest.mark.asyncio
async def test_flag_on_but_not_entitled(monkeypatch):
    monkeypatch.setenv("WADDLEAI_FLAG_PROVIDER_FAILOVER", "1")
    gate = FailoverGate(license_getter=_lic(False))
    assert await gate.evaluate(7) == (False, "not_entitled")


@pytest.mark.asyncio
async def test_enabled_when_flag_and_entitlement(monkeypatch):
    monkeypatch.setenv("WADDLEAI_FLAG_PROVIDER_FAILOVER", "1")
    gate = FailoverGate(license_getter=_lic(True))
    assert await gate.evaluate(7) == (True, "ok")


@pytest.mark.asyncio
async def test_entitlement_error_is_fail_closed(monkeypatch):
    monkeypatch.setenv("WADDLEAI_FLAG_PROVIDER_FAILOVER", "1")
    client = MagicMock(); client.check_feature.side_effect = RuntimeError("license down")
    gate = FailoverGate(license_getter=lambda: client)
    assert await gate.evaluate(7) == (False, "not_entitled")


@pytest.mark.asyncio
async def test_memoised_per_org_within_ttl(monkeypatch):
    monkeypatch.setenv("WADDLEAI_FLAG_PROVIDER_FAILOVER", "1")
    clock = _Clock()
    client = MagicMock(); client.check_feature.return_value = True
    gate = FailoverGate(license_getter=lambda: client, ttl_seconds=60.0, clock=clock)
    await gate.evaluate(7); await gate.evaluate(7)
    assert client.check_feature.call_count == 1        # cached
    clock.tick(61)
    await gate.evaluate(7)
    assert client.check_feature.call_count == 2        # re-checked after TTL
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/routing/test_failover_gate.py -v --no-cov`
Expected: FAIL — module not defined.

- [ ] **Step 3: Write minimal implementation**

`shared/routing/failover_gate.py`:
```python
"""Two-layer gate for provider-destination failover on the proxy hot path (spec §5.1/§7).

PostHog flag ``waddleai.provider_failover`` (default OFF, fail-safe) AND Enterprise
entitlement ``waddleai_provider_failover`` (fail-closed). Result memoised per org for
``ttl_seconds`` so the request path never blocks on the license server; a lapsed
entitlement degrades to today's behaviour, never to an error.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Callable

from shared.utils.feature_flags import is_feature_enabled

logger = logging.getLogger(__name__)

_FLAG_KEY = "waddleai.provider_failover"
_LICENSE_FEATURE = "waddleai_provider_failover"


def _default_license_getter() -> Any:
    from penguin_licensing import LicenseClient

    return LicenseClient(
        license_key=os.environ.get("LICENSE_KEY", ""),
        product="waddleai",
        base_url=os.environ.get("LICENSE_SERVER_URL", "https://license.penguintech.io"),
    )


class FailoverGate:
    """Flag + Enterprise entitlement check, memoised per org (spec §5.1)."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 60.0,
        license_getter: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind TTL, an injectable license-client getter, and an injectable clock."""
        self._ttl = ttl_seconds
        self._license_getter = license_getter or _default_license_getter
        self._clock = clock
        self._cache: dict[int, tuple[float, tuple[bool, str]]] = {}

    async def evaluate(self, org_id: int) -> tuple[bool, str]:
        """Return (enabled, reason); reason in ok|flag_off|not_entitled. Never raises."""
        cached = self._cache.get(org_id)
        now = self._clock()
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        result = await self._compute(org_id)
        self._cache[org_id] = (now, result)
        return result

    async def _compute(self, org_id: int) -> tuple[bool, str]:
        if not is_feature_enabled(_FLAG_KEY, distinct_id=str(org_id), default=False):
            return (False, "flag_off")

        def _check() -> bool:
            try:
                return bool(self._license_getter().check_feature(_LICENSE_FEATURE))
            except Exception as exc:  # pragma: no cover - defensive, license I/O failure
                logger.warning("failover_gate: entitlement check failed (fail-closed): %s", exc)
                return False

        entitled = await asyncio.to_thread(_check)
        return (True, "ok") if entitled else (False, "not_entitled")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/routing/test_failover_gate.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/routing/failover_gate.py tests/unit/routing/test_failover_gate.py
git commit -m "feat(failover): flag+entitlement gate, memoised per org" \
  -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" \
  -m "Claude-Session: https://claude.ai/code/session_013GYHFHJZgh6t5u1v3hTb14"
```

---

### Task 7: Pool-exclusion of tenant-owned credentials (security-critical: S3, mutation-proven)

**Files:**
- Modify: `shared/utils/llm_connectors.py` (`_select_credential` pool query)
- Test: `tests/unit/test_llm_connectors_pool_exclusion.py`

**Interfaces:**
- Consumes: `provider_credentials.owner_org_id` (Task 1). Depends on Task 3 (same file — different wave).
- Produces: `_select_credential` adds `owner_org_id IS NULL` to its pool query, so a tenant-owned (BYOK) key can never be selected for platform traffic. No new public signature.

**Rule reminder:** in the management context `db.provider_credentials` is reflected from the SQLAlchemy model (Task 1), so `db.provider_credentials.owner_org_id` resolves. Reference `owner_org_id`, never `org_id` (the provider workspace column).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_llm_connectors_pool_exclusion.py
from __future__ import annotations
from shared.utils.llm_connectors import LLMConnectionManager


class _Field:
    def __init__(self, name): self.name = name
    def __eq__(self, other): return ("eq", self.name, other)


class _CredTable:
    provider_id = _Field("provider_id")
    enabled = _Field("enabled")
    owner_org_id = _Field("owner_org_id")


class _ProvTable:
    name = _Field("name")
    id = _Field("id")


class _Row:
    def __init__(self, **kw): self.__dict__.update(kw)


class _Query:
    def __init__(self, db, expr): self.db = db; self.expr = expr
    def select(self): return self.db._resolve(self.expr)


class _FakeDB:
    """Records the predicate tree passed to db(...); returns only pool rows the predicate admits."""
    def __init__(self, cred_rows):
        self.provider_credentials = _CredTable()
        self.ai_providers = _ProvTable()
        self._cred_rows = cred_rows
        self.captured_expr = None

    def __call__(self, expr): return _Query(self, expr)

    def _flatten(self, expr, acc):
        # penguin-dal ANDs compose as nested tuples via &; capture leaf ("eq", field, val)
        if isinstance(expr, tuple) and expr and expr[0] == "eq":
            acc.append(expr); return
        if isinstance(expr, (list, tuple)):
            for e in expr:
                self._flatten(e, acc)

    def _resolve(self, expr):
        # provider lookup (name == "prov") returns the provider row
        leaves = []; self._flatten(expr, leaves)
        if ("eq", "name", "prov") in [(l[0], l[1], l[2]) for l in leaves]:
            return [_Row(id=1)]
        self.captured_expr = leaves
        admits_null_only = ("eq", "owner_org_id", None) in leaves
        rows = []
        for r in self._cred_rows:
            if admits_null_only and r.owner_org_id is not None:
                continue
            rows.append(r)
        return rows


def _mk_link():
    return _Row(name="prov", api_key="platform-key", enabled=True)


def test_byok_credential_is_excluded_from_pool():
    rows = [
        _Row(id=10, label="byok", api_key="tenant-key", org_id="", owner_org_id=99, weight=100),
        _Row(id=11, label="platform", api_key="pool-key", org_id="", owner_org_id=None, weight=100),
    ]
    db = _FakeDB(rows)
    mgr = LLMConnectionManager.__new__(LLMConnectionManager)   # bypass _load_connectors
    mgr.db = db
    from shared.utils.llm_connectors import RoundRobinSelector
    mgr._selector = RoundRobinSelector()
    key = mgr._select_credential(_mk_link())
    assert key == "pool-key"                       # never the tenant-owned key
    assert ("eq", "owner_org_id", None) in db.captured_expr   # predicate present
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_llm_connectors_pool_exclusion.py -v --no-cov`
Expected: FAIL — the pool query lacks the `owner_org_id IS NULL` predicate, so the BYOK row is admitted (`captured_expr` assertion fails).

- [ ] **Step 3: Write minimal implementation**

In `_select_credential`, add the `owner_org_id IS NULL` predicate to the pool query (lines ~2069–2072):
```python
            cred_rows = self.db(
                (self.db.provider_credentials.provider_id == provider_row.id)
                & (self.db.provider_credentials.enabled == True)  # noqa: E712
                & (self.db.provider_credentials.owner_org_id == None)  # noqa: E711 -- BYOK excluded (S3)
            ).select()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_llm_connectors_pool_exclusion.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Mutation-proof the guard (S3)**

Temporarily delete the `& (self.db.provider_credentials.owner_org_id == None)` line, re-run:

Run: `.venv/bin/pytest tests/unit/test_llm_connectors_pool_exclusion.py -v --no-cov`
Expected: **FAIL** — `key == "pool-key"` no longer holds (round-robin now also admits the BYOK row) and/or the `captured_expr` assertion fails. This proves the test actually gates the predicate. **Restore the line** and re-run:

Run: `.venv/bin/pytest tests/unit/test_llm_connectors_pool_exclusion.py -v --no-cov`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add shared/utils/llm_connectors.py tests/unit/test_llm_connectors_pool_exclusion.py
git commit -m "fix(failover): exclude tenant-owned credentials from the platform pool (S3)" \
  -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" \
  -m "Claude-Session: https://claude.ai/code/session_013GYHFHJZgh6t5u1v3hTb14"
```

---

### Task 8: `DestinationResolver` (security-critical: S2 resolve predicate, S8 cross-org, S9 pin/local_only)

**Files:**
- Create: `shared/routing/destinations.py`
- Test: `tests/unit/routing/test_destination_resolver.py`

**Interfaces:**
- Consumes: a penguin-dal `db` with `executesql`.
- Produces: `Destination`, `CredentialMaterial`, `DestinationResolver.resolve(...)`, `DestinationResolver.load_material(...)` (signatures in Key Interfaces). Consumed by Tasks 9 (registry `credential_loader = resolver.load_material`), 14 (`DispatchStage`), 16 (harness).

**Deviation from spec §3.2 (documented, orchestrator to adjudicate):** the resolver reads via **parameterized `db.executesql`** (the `shared/graph/resolver.py` precedent), not a proxy PyDAL mirror. Reason: the read must JOIN `provider_credentials`, which the proxy deliberately does **not** mirror (`shared/database/models.py:184` — credential material must not enter the proxy's PyDAL schema). `executesql` reads the live columns without mirroring any credential field into PyDAL. This is *stronger* than the spec wording (no credential columns in the proxy schema) and matches the established graph-resolver pattern.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/routing/test_destination_resolver.py
from __future__ import annotations
import pytest
from shared.routing.destinations import Destination, DestinationResolver


# One joined row shape (mirrors the SELECT column order in the impl):
# (id, organization_id, model, priority, provider_id, provider_type, endpoint_url,
#  provider_extra_config, provider_model_id, region, timeout_seconds, credential_id,
#  owner_org_id, updated_at)
def _row(**kw):
    base = dict(id=1, organization_id=7, model="claude-sonnet-4", priority=0, provider_id=3,
                provider_type="bedrock", endpoint_url=None, provider_extra_config=None,
                provider_model_id="anthropic.claude-sonnet-4-v1:0", region="us-west-2",
                timeout_seconds=None, credential_id=5, owner_org_id=7, updated_at="2026-09-04T00:00:00")
    base.update(kw)
    return tuple(base[k] for k in (
        "id", "organization_id", "model", "priority", "provider_id", "provider_type",
        "endpoint_url", "provider_extra_config", "provider_model_id", "region",
        "timeout_seconds", "credential_id", "owner_org_id", "updated_at"))


class _FakeDB:
    def __init__(self, rows): self._rows = rows; self.calls = []
    def executesql(self, sql, params=None):
        self.calls.append((sql, params)); return list(self._rows)


class _Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def tick(self, dt): self.t += dt


@pytest.mark.asyncio
async def test_resolve_maps_rows_and_defaults_timeout():
    db = _FakeDB([_row()])
    dests = await DestinationResolver(db).resolve(7, "claude-sonnet-4")
    assert len(dests) == 1
    d = dests[0]
    assert isinstance(d, Destination)
    assert d.timeout_seconds == 30                 # NULL -> 30 default
    assert d.provider_type == "bedrock" and d.role == "active"
    assert d.credential_version == "2026-09-04T00:00:00"
    # org_id + model bound as params (S8 — never interpolated)
    _sql, params = db.calls[0]
    assert 7 in params and "claude-sonnet-4" in params


@pytest.mark.asyncio
async def test_pin_keeps_only_matching_provider():
    db = _FakeDB([
        _row(id=1, provider_type="bedrock", priority=0),
        _row(id=2, provider_type="ollama", priority=1),
    ])
    dests = await DestinationResolver(db).resolve(7, "m", pin="ollama")
    assert [d.id for d in dests] == [2]


@pytest.mark.asyncio
async def test_local_only_keeps_only_local_providers():
    db = _FakeDB([
        _row(id=1, provider_type="bedrock", priority=0),
        _row(id=2, provider_type="llamacpp", priority=1),
        _row(id=3, provider_type="ollama", priority=2),
    ])
    dests = await DestinationResolver(db).resolve(7, "m", local_only=True)
    assert sorted(d.id for d in dests) == [2, 3]


@pytest.mark.asyncio
async def test_region_falls_back_to_provider_extra_config():
    db = _FakeDB([_row(region=None, provider_extra_config='{"region": "eu-central-1"}')])
    dests = await DestinationResolver(db).resolve(7, "m")
    assert dests[0].region == "eu-central-1"


@pytest.mark.asyncio
async def test_ttl_cache_is_keyed_by_org_and_model():
    clock = _Clock()
    db = _FakeDB([_row()])
    r = DestinationResolver(db, ttl_seconds=30.0, clock=clock)
    await r.resolve(7, "m"); await r.resolve(7, "m")
    assert len(db.calls) == 1                       # cached within TTL
    await r.resolve(8, "m")                         # different org -> new read (S8)
    assert len(db.calls) == 2
    clock.tick(31)
    await r.resolve(7, "m")
    assert len(db.calls) == 3                       # TTL expired


@pytest.mark.asyncio
async def test_load_material_returns_secret_bearing_row():
    class _DB:
        def executesql(self, sql, params=None):
            return [(5, 3, 7, "enc:xxxxx", "2026-09-04T00:00:00")]
    mat = await DestinationResolver(_DB()).load_material(5)
    assert mat.credential_id == 5 and mat.owner_org_id == 7
    assert mat.encrypted_material == "enc:xxxxx"
    assert "enc:xxxxx" not in repr(mat)             # S4 — secret excluded from repr
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/routing/test_destination_resolver.py -v --no-cov`
Expected: FAIL — module not defined.

- [ ] **Step 3: Write minimal implementation**

`shared/routing/destinations.py`:
```python
"""Destination resolution for provider failover (spec §5.2).

One parameterized executesql JOIN (model_destinations -> ai_providers ->
provider_credentials) filtered by org + model + enabled AND the §3.2 ownership
predicate; ordered by priority; TTL-cached (30 s) keyed (org_id, model). The
Destination value type NEVER carries the secret; the registry loads material on
demand via load_material(). See the module for the executesql deviation rationale.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

_LOCAL_PROVIDERS = frozenset({"ollama", "llamacpp"})
_DEFAULT_TIMEOUT_SECONDS = 30

_RESOLVE_SQL = """
SELECT d.id, d.organization_id, d.model, d.priority, d.provider_id,
       p.provider_type, p.endpoint_url, p.extra_config,
       d.provider_model_id, d.region, d.timeout_seconds,
       d.credential_id, c.owner_org_id, c.updated_at
FROM model_destinations d
JOIN ai_providers p ON p.id = d.provider_id AND p.enabled = TRUE
LEFT JOIN provider_credentials c ON c.id = d.credential_id
WHERE d.organization_id = %s AND d.model = %s AND d.enabled = TRUE
  AND (c.id IS NULL OR c.owner_org_id IS NULL OR c.owner_org_id = d.organization_id)
ORDER BY d.priority ASC
"""

_MATERIAL_SQL = """
SELECT id, provider_id, owner_org_id, api_key, updated_at
FROM provider_credentials WHERE id = %s LIMIT 1
"""


@dataclass(slots=True, frozen=True)
class Destination:
    """One resolved active/standby destination. NEVER carries the secret (spec §5.2)."""

    id: int
    organization_id: int
    model: str
    priority: int
    provider_id: int
    provider_type: str
    endpoint_url: str | None
    region: str | None
    provider_model_id: str | None
    timeout_seconds: int
    credential_id: int | None
    owner_org_id: int | None
    credential_version: str

    @property
    def role(self) -> str:
        """'active' for priority 0, else 'standby'."""
        return "active" if self.priority == 0 else "standby"


@dataclass(slots=True, frozen=True)
class CredentialMaterial:
    """Secret-bearing credential row loaded on demand by the registry only (never cached/logged)."""

    credential_id: int
    provider_id: int
    owner_org_id: int | None
    encrypted_material: str = field(repr=False, default="")
    updated_at: str | None = None


def _region_of(dest_region: Any, extra_config: Any) -> str | None:
    if dest_region:
        return dest_region
    if isinstance(extra_config, str) and extra_config:
        try:
            extra_config = json.loads(extra_config)
        except ValueError:
            return None
    if isinstance(extra_config, dict):
        return extra_config.get("region")
    return None


class DestinationResolver:
    """Reads and caches the ordered destination list for (org, logical model)."""

    def __init__(
        self, db: Any, *, ttl_seconds: float = 30.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        """Bind the DB handle; configure the in-process TTL cache and injectable clock."""
        self._db = db
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[tuple[int, str], tuple[float, list[Destination]]] = {}

    async def resolve(
        self, org_id: int, model: str, *, pin: str | None = None, local_only: bool = False
    ) -> list[Destination]:
        """Ordered enabled destinations for (org, model), pin/local_only filtered (spec §5.2)."""
        dests = await self._resolve_all(org_id, model)
        if pin:
            dests = [d for d in dests if d.provider_type == pin]
        if local_only:
            dests = [d for d in dests if d.provider_type in _LOCAL_PROVIDERS]
        return dests

    async def _resolve_all(self, org_id: int, model: str) -> list[Destination]:
        key = (org_id, model)
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]

        def _read() -> list[tuple]:
            return list(self._db.executesql(_RESOLVE_SQL, [org_id, model]))

        rows = await asyncio.to_thread(_read)
        dests = [
            Destination(
                id=r[0], organization_id=r[1], model=r[2], priority=r[3], provider_id=r[4],
                provider_type=r[5], endpoint_url=r[6],
                region=_region_of(r[9], r[7]),
                provider_model_id=r[8],
                timeout_seconds=r[10] if r[10] is not None else _DEFAULT_TIMEOUT_SECONDS,
                credential_id=r[11], owner_org_id=r[12],
                credential_version=str(r[13]) if r[13] is not None else "",
            )
            for r in rows
        ]
        self._cache[key] = (now, dests)
        return dests

    async def load_material(self, credential_id: int) -> CredentialMaterial | None:
        """Load a credential's secret-bearing row for the registry (never cached here)."""
        def _read() -> tuple | None:
            rows = self._db.executesql(_MATERIAL_SQL, [credential_id])
            return rows[0] if rows else None

        row = await asyncio.to_thread(_read)
        if row is None:
            return None
        return CredentialMaterial(
            credential_id=row[0], provider_id=row[1], owner_org_id=row[2],
            encrypted_material=row[3] or "", updated_at=str(row[4]) if row[4] is not None else None,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/routing/test_destination_resolver.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/routing/destinations.py tests/unit/routing/test_destination_resolver.py
git commit -m "feat(failover): DestinationResolver (executesql join, TTL cache, pin/local_only)" \
  -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" \
  -m "Claude-Session: https://claude.ai/code/session_013GYHFHJZgh6t5u1v3hTb14"
```

---

### Task 9: `DestinationConnectorRegistry` (security-critical: S2 build assert, S4 material)

**Files:**
- Create: `shared/routing/destination_connectors.py`
- Test: `tests/unit/routing/test_destination_connectors.py`

**Interfaces:**
- Consumes: `Destination`/`CredentialMaterial` (Task 8); `decrypt_credential` (existing); the existing connector classes (`OpenAIConnector`, `AnthropicConnector`, `BedrockConnector`, `XAIConnector`, `GeminiConnector`, `OllamaConnector`, `LlamaCppConnector`) from `shared.utils.llm_connectors`.
- Produces: `OwnershipError`, `DestinationConnectorRegistry.get(dest) -> LLMConnector` (raises `OwnershipError` on S2 mismatch). Builds one connector per distinct `(provider_id, credential_id, credential_version, endpoint_url, region)`, LRU 256 / 15-min idle; a rotated credential (new `credential_version`) yields a new key so the old client is dropped. Consumed by Task 10 (dispatcher).

**Rule reminders:** the connector `config` is `{endpoint_url, api_key: decrypt_credential(material), aws_region, model_list: []}`; secrets live only inside the connector instance; the registry never logs `config`; ownership is re-asserted at build (S2 #3): the loaded credential's `owner_org_id` must be `None` or `== dest.organization_id`, else `OwnershipError` and skip.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/routing/test_destination_connectors.py
from __future__ import annotations
import pytest
from shared.routing.destinations import CredentialMaterial, Destination
from shared.routing.destination_connectors import DestinationConnectorRegistry, OwnershipError


def _dest(**kw):
    base = dict(id=1, organization_id=7, model="m", priority=0, provider_id=3, provider_type="openai",
                endpoint_url="http://127.0.0.1:9/v1", region=None, provider_model_id=None,
                timeout_seconds=30, credential_id=5, owner_org_id=7, credential_version="v1")
    base.update(kw)
    return Destination(**base)


def _loader(material_by_id):
    async def load(cid):
        return material_by_id.get(cid)
    return load


class _Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def tick(self, dt): self.t += dt


@pytest.mark.asyncio
async def test_builds_openai_connector_with_decrypted_key(monkeypatch):
    built = {}

    class _FakeOpenAI:
        def __init__(self, name, config): built["config"] = config; self.name = name
    import shared.routing.destination_connectors as mod
    monkeypatch.setitem(mod._CONNECTOR_CLASSES, "openai", _FakeOpenAI)
    reg = DestinationConnectorRegistry(
        _loader({5: CredentialMaterial(5, 3, 7, encrypted_material="plain-key")})
    )
    conn = await reg.get(_dest())
    assert isinstance(conn, _FakeOpenAI)
    assert built["config"]["api_key"] == "plain-key"       # decrypt passthrough for non-enc value
    assert built["config"]["endpoint_url"] == "http://127.0.0.1:9/v1"


@pytest.mark.asyncio
async def test_same_version_reuses_instance(monkeypatch):
    calls = {"n": 0}

    class _FakeOpenAI:
        def __init__(self, name, config): calls["n"] += 1
    import shared.routing.destination_connectors as mod
    monkeypatch.setitem(mod._CONNECTOR_CLASSES, "openai", _FakeOpenAI)
    reg = DestinationConnectorRegistry(_loader({5: CredentialMaterial(5, 3, 7, encrypted_material="k")}))
    await reg.get(_dest()); await reg.get(_dest())
    assert calls["n"] == 1                                   # reused


@pytest.mark.asyncio
async def test_rotated_version_rebuilds(monkeypatch):
    calls = {"n": 0}

    class _FakeOpenAI:
        def __init__(self, name, config): calls["n"] += 1
    import shared.routing.destination_connectors as mod
    monkeypatch.setitem(mod._CONNECTOR_CLASSES, "openai", _FakeOpenAI)
    reg = DestinationConnectorRegistry(_loader({5: CredentialMaterial(5, 3, 7, encrypted_material="k")}))
    await reg.get(_dest(credential_version="v1"))
    await reg.get(_dest(credential_version="v2"))
    assert calls["n"] == 2                                   # new version -> new client


@pytest.mark.asyncio
async def test_ownership_mismatch_raises(monkeypatch):
    # credential owned by org 99 but destination is org 7 -> S2 build assert fails
    reg = DestinationConnectorRegistry(_loader({5: CredentialMaterial(5, 3, 99, encrypted_material="k")}))
    with pytest.raises(OwnershipError):
        await reg.get(_dest(organization_id=7, owner_org_id=99))


@pytest.mark.asyncio
async def test_platform_credential_null_owner_is_allowed(monkeypatch):
    class _FakeOpenAI:
        def __init__(self, name, config): pass
    import shared.routing.destination_connectors as mod
    monkeypatch.setitem(mod._CONNECTOR_CLASSES, "openai", _FakeOpenAI)
    reg = DestinationConnectorRegistry(_loader({5: CredentialMaterial(5, 3, None, encrypted_material="k")}))
    conn = await reg.get(_dest(owner_org_id=None))
    assert conn is not None


@pytest.mark.asyncio
async def test_null_credential_id_builds_ambient(monkeypatch):
    built = {}

    class _FakeBedrock:
        def __init__(self, name, config): built["config"] = config
    import shared.routing.destination_connectors as mod
    monkeypatch.setitem(mod._CONNECTOR_CLASSES, "bedrock", _FakeBedrock)
    reg = DestinationConnectorRegistry(_loader({}))          # no material needed
    await reg.get(_dest(provider_type="bedrock", credential_id=None, owner_org_id=None,
                        region="us-east-2"))
    assert built["config"]["api_key"] in ("", None)          # ambient chain
    assert built["config"]["aws_region"] == "us-east-2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/routing/test_destination_connectors.py -v --no-cov`
Expected: FAIL — module not defined.

- [ ] **Step 3: Write minimal implementation**

`shared/routing/destination_connectors.py`:
```python
"""Per-credential connector registry for provider failover (spec §5.5).

Builds an LLMConnector per distinct (provider_id, credential_id, credential_version,
endpoint_url, region) using the EXISTING connector classes; the decrypted secret lives
only inside the connector instance (never logged, never in repr). Ownership is
re-asserted at build (S2 #3). Bounded LRU (256) with idle eviction; a rotated
credential (new version) yields a new cache key so the stale client is dropped.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable

from shared.routing.destinations import CredentialMaterial, Destination
from shared.security.credential_encryption import decrypt_credential
from shared.utils.llm_connectors import (
    AnthropicConnector, BedrockConnector, GeminiConnector, LLMConnector, LlamaCppConnector,
    OllamaConnector, OpenAIConnector, XAIConnector,
)

logger = logging.getLogger(__name__)

_CONNECTOR_CLASSES: dict[str, type[LLMConnector]] = {
    "openai": OpenAIConnector,
    "xai": XAIConnector,
    "anthropic": AnthropicConnector,
    "gemini": GeminiConnector,
    "ollama": OllamaConnector,
    "llamacpp": LlamaCppConnector,
    "bedrock": BedrockConnector,
    "azure_openai": OpenAIConnector,
    "cohere": OpenAIConnector,
}


class OwnershipError(Exception):
    """The loaded credential is owned by a different org than the destination (S2)."""


class DestinationConnectorRegistry:
    """Builds and reuses connectors keyed by credential version; asserts ownership at build."""

    def __init__(
        self,
        credential_loader: Callable[[int], Awaitable[CredentialMaterial | None]],
        *,
        max_size: int = 256,
        idle_seconds: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the credential loader (registry never reads the DB directly) + LRU bounds."""
        self._load = credential_loader
        self._max = max_size
        self._idle = idle_seconds
        self._clock = clock
        self._cache: "OrderedDict[tuple, tuple[float, LLMConnector]]" = OrderedDict()

    @staticmethod
    def _key(dest: Destination) -> tuple:
        return (dest.provider_id, dest.credential_id, dest.credential_version,
                dest.endpoint_url, dest.region)

    def _evict_idle(self, now: float) -> None:
        stale = [k for k, (ts, _) in self._cache.items() if now - ts > self._idle]
        for k in stale:
            self._cache.pop(k, None)

    async def get(self, dest: Destination) -> LLMConnector:
        """Return a connector for ``dest``; build+cache on miss. Raises OwnershipError on S2 mismatch."""
        now = self._clock()
        self._evict_idle(now)
        key = self._key(dest)
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            self._cache[key] = (now, hit[1])
            return hit[1]

        connector = await self._build(dest)
        self._cache[key] = (now, connector)
        self._cache.move_to_end(key)
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)
        return connector

    async def _build(self, dest: Destination) -> LLMConnector:
        api_key: str = ""
        if dest.credential_id is not None:
            material = await self._load(dest.credential_id)
            if material is None:
                raise OwnershipError(f"credential {dest.credential_id} not found for dest {dest.id}")
            if material.owner_org_id is not None and material.owner_org_id != dest.organization_id:
                raise OwnershipError(
                    f"credential {dest.credential_id} owner {material.owner_org_id} "
                    f"!= dest org {dest.organization_id}"
                )
            api_key = decrypt_credential(material.encrypted_material or "")

        cls = _CONNECTOR_CLASSES.get(dest.provider_type)
        if cls is None:
            raise OwnershipError(f"unsupported provider_type {dest.provider_type!r}")
        config = {
            "enabled": True,
            "endpoint_url": dest.endpoint_url,
            "api_key": api_key,
            "aws_region": dest.region,
            "model_list": [],
        }
        return cls(f"dest:{dest.id}", config)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/routing/test_destination_connectors.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/routing/destination_connectors.py tests/unit/routing/test_destination_connectors.py
git commit -m "feat(failover): per-credential connector registry with ownership assert" \
  -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" \
  -m "Claude-Session: https://claude.ai/code/session_013GYHFHJZgh6t5u1v3hTb14"
```

---

### Task 10: `FailoverDispatcher` (security-critical: S5 retryable-only, S6 first-byte, S7 bounded)

**Files:**
- Create: `shared/routing/failover.py`
- Test: `tests/unit/routing/test_failover_dispatcher.py`

**Interfaces:**
- Consumes: `Destination` (Task 8), `DestinationConnectorRegistry`/`OwnershipError` (Task 9), `DestinationBreaker` (Task 4), `ProviderError` taxonomy + `is_retryable`/`classify_failure` (Task 3), `WaddleAIMetrics` (Task 5).
- Produces: `DestinationAttempt`, `Outcome` (+ `.marker`), `DestinationsExhausted` (+ `.status_code()`/`.retry_after()`), `FailoverDispatcher.dispatch(ctx, destinations, messages) -> Outcome`. Consumed by Task 14 (`DispatchStage`), 16 (harness).

**Rule reminders (spec §5.3/§5.4):** breaker-open + no probe → skip; ownership failure → skip+log; one attempt = one connector call with `model = dest.provider_model_id or ctx.model`; `attempt()` catches `asyncio.TimeoutError`→`ProviderTimeoutError` and wraps connection errors→`ProviderServerError` so only `ProviderError` escapes; retryable → record failure, next; `ctx.bytes_flushed` True → re-raise (first-byte rule); `ProviderClientError` (4xx) → raise immediately, never trip the breaker; `timeout_seconds` bounds total time (non-stream) / time-to-first-chunk (stream).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/routing/test_failover_dispatcher.py
from __future__ import annotations
import asyncio
from types import SimpleNamespace
import pytest
from shared.routing.destinations import Destination
from shared.routing.destination_breaker import DestinationBreaker
from shared.routing.failover import DestinationsExhausted, FailoverDispatcher
from shared.utils.llm_connectors import (
    ProviderClientError, ProviderRateLimitError, ProviderServerError,
)


def _dest(i, provider="openai", model_id=None):
    return Destination(id=i, organization_id=7, model="m", priority=i, provider_id=i,
                       provider_type=provider, endpoint_url=None, region=None,
                       provider_model_id=model_id, timeout_seconds=30, credential_id=i,
                       owner_org_id=7, credential_version="v1")


class _Connector:
    def __init__(self, *, text=None, exc=None, hang=False):
        self._text, self._exc, self._hang = text, exc, hang
    async def chat_completion(self, messages, model, **kw):
        if self._hang:
            await asyncio.sleep(10)
        if self._exc:
            raise self._exc
        return self._text, {"input_tokens": 1, "output_tokens": 2, "finish_reason": "stop"}
    async def stream_chat_completion(self, messages, model, **kw):
        if self._hang:
            await asyncio.sleep(10)
        if self._exc:
            raise self._exc
        yield SimpleNamespace(delta=self._text or "", usage={"finish_reason": "stop"}, done=True)


class _Registry:
    def __init__(self, mapping): self._m = mapping
    async def get(self, dest): return self._m[dest.id]


def _ctx(stream=False, bytes_flushed=False):
    return SimpleNamespace(model="m", stream=stream, bytes_flushed=bytes_flushed)


@pytest.mark.asyncio
async def test_active_success_returns_first():
    reg = _Registry({1: _Connector(text="hi")})
    out = await FailoverDispatcher(reg, DestinationBreaker()).dispatch(_ctx(), [_dest(1)], ["m"])
    assert out.text == "hi" and out.destination.id == 1
    assert out.attempts[-1].outcome == "ok"


@pytest.mark.asyncio
async def test_retryable_fails_over_to_standby():
    reg = _Registry({
        1: _Connector(exc=ProviderServerError("openai", "m", "503", status_code=503)),
        2: _Connector(text="from-standby"),
    })
    out = await FailoverDispatcher(reg, DestinationBreaker()).dispatch(
        _ctx(), [_dest(1), _dest(2)], ["m"]
    )
    assert out.text == "from-standby" and out.destination.id == 2
    assert out.attempts[0].outcome == "failed" and out.attempts[0].reason == "server_error"


@pytest.mark.asyncio
async def test_client_error_never_fails_over_and_is_raised():
    reg = _Registry({
        1: _Connector(exc=ProviderClientError("openai", "m", "401", status_code=401)),
        2: _Connector(text="should-not-run"),
    })
    with pytest.raises(ProviderClientError):
        await FailoverDispatcher(reg, DestinationBreaker()).dispatch(
            _ctx(), [_dest(1), _dest(2)], ["m"]
        )


@pytest.mark.asyncio
async def test_client_error_does_not_trip_breaker():
    breaker = DestinationBreaker()
    reg = _Registry({1: _Connector(exc=ProviderClientError("openai", "m", "400", status_code=400))})
    with pytest.raises(ProviderClientError):
        await FailoverDispatcher(reg, breaker).dispatch(_ctx(), [_dest(1)], ["m"])
    assert breaker.is_open(1) is False
    assert "dest:1" not in breaker.snapshot() or breaker.snapshot()["dest:1"]["consecutive_failures"] == 0


@pytest.mark.asyncio
async def test_all_retryable_raises_destinations_exhausted_with_last_status():
    reg = _Registry({
        1: _Connector(exc=ProviderServerError("openai", "m", "503", status_code=503)),
        2: _Connector(exc=ProviderRateLimitError("openai", "m", "429", status_code=429)),
    })
    with pytest.raises(DestinationsExhausted) as ei:
        await FailoverDispatcher(reg, DestinationBreaker()).dispatch(_ctx(), [_dest(1), _dest(2)], ["m"])
    assert ei.value.status_code() == 429                # last retryable was a 429


@pytest.mark.asyncio
async def test_destinations_exhausted_retry_after_from_rate_limit_error():
    reg = _Registry({
        1: _Connector(exc=ProviderServerError("openai", "m", "503", status_code=503)),
        2: _Connector(exc=ProviderRateLimitError("openai", "m", "429", status_code=429, retry_after=12.5)),
    })
    with pytest.raises(DestinationsExhausted) as ei:
        await FailoverDispatcher(reg, DestinationBreaker()).dispatch(_ctx(), [_dest(1), _dest(2)], ["m"])
    assert ei.value.retry_after() == 12.5                # last error's retry_after, unit-preserved


@pytest.mark.asyncio
async def test_destinations_exhausted_retry_after_none_for_non_rate_limit():
    reg = _Registry({1: _Connector(exc=ProviderServerError("openai", "m", "503", status_code=503))})
    with pytest.raises(DestinationsExhausted) as ei:
        await FailoverDispatcher(reg, DestinationBreaker()).dispatch(_ctx(), [_dest(1)], ["m"])
    assert ei.value.retry_after() is None                # last error wasn't a rate limit


@pytest.mark.asyncio
async def test_breaker_open_destination_is_skipped():
    breaker = DestinationBreaker(failure_threshold=1, cooldown_seconds=300)
    breaker.record_failure(1)                            # trip active
    reg = _Registry({1: _Connector(text="nope"), 2: _Connector(text="served")})
    out = await FailoverDispatcher(reg, breaker).dispatch(_ctx(), [_dest(1), _dest(2)], ["m"])
    assert out.destination.id == 2
    assert out.attempts[0].outcome == "skipped" and out.attempts[0].reason == "breaker_open"


@pytest.mark.asyncio
async def test_timeout_is_classified_and_fails_over():
    reg = _Registry({1: _Connector(hang=True), 2: _Connector(text="served")})
    d1 = _dest(1); object.__setattr__(d1, "timeout_seconds", 1)  # bound the hang low for the test
    out = await FailoverDispatcher(reg, DestinationBreaker()).dispatch(_ctx(), [d1, _dest(2)], ["m"])
    assert out.destination.id == 2
    assert out.attempts[0].reason == "timeout"


@pytest.mark.asyncio
async def test_first_byte_rule_reraises_without_failover():
    reg = _Registry({
        1: _Connector(exc=ProviderServerError("openai", "m", "503", status_code=503)),
        2: _Connector(text="standby"),
    })
    with pytest.raises(ProviderServerError):
        await FailoverDispatcher(reg, DestinationBreaker()).dispatch(
            _ctx(bytes_flushed=True), [_dest(1), _dest(2)], ["m"]
        )


@pytest.mark.asyncio
async def test_marker_shape_is_secret_free():
    reg = _Registry({1: _Connector(text="hi")})
    out = await FailoverDispatcher(reg, DestinationBreaker()).dispatch(_ctx(), [_dest(1, model_id="x")], ["m"])
    marker = out.marker
    assert marker["role"] == "active" and marker["provider"] == "openai"
    assert "attempts" in marker and marker["attempts"][0]["outcome"] == "ok"
    assert "endpoint" not in marker and "credential_id" not in marker
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/routing/test_failover_dispatcher.py -v --no-cov`
Expected: FAIL — module not defined.

- [ ] **Step 3: Write minimal implementation**

`shared/routing/failover.py`:
```python
"""The failover dispatcher — walks an ordered destination list (spec §5.3/§5.4).

Retryable failure -> next destination (records breaker failure); client error (4xx) ->
raise, never fail over, never trip the breaker; breaker-open / ownership failure ->
skip+log. One attempt = one connector call bounded by dest.timeout_seconds (total time
for non-streaming, time-to-first-chunk for streaming). Failover is legal only before the
first flushed byte (ctx.bytes_flushed).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from shared.routing.destination_breaker import DestinationBreaker
from shared.routing.destination_connectors import DestinationConnectorRegistry, OwnershipError
from shared.routing.destinations import Destination
from shared.utils.llm_connectors import (
    ProviderClientError, ProviderError, ProviderRateLimitError, ProviderServerError,
    ProviderTimeoutError, classify_failure, is_retryable,
)

logger = logging.getLogger(__name__)

_STATUS_BY_REASON = {"rate_limit": 429, "timeout": 504, "server_error": 502}


@dataclass(slots=True, frozen=True)
class DestinationAttempt:
    """One attempt against one destination (named to avoid llm_connectors.AttemptRecord)."""

    destination_id: int
    provider: str
    outcome: str
    reason: str | None = None


@dataclass(slots=True, frozen=True)
class Outcome:
    """A successful destination result plus the full attempt trail (spec §5.7)."""

    destination: Destination
    provider_type: str
    text: str
    usage: dict = field(repr=False, default_factory=dict)
    finish_reason: str = "stop"
    attempts: tuple[DestinationAttempt, ...] = ()

    @property
    def marker(self) -> dict:
        """usage.waddleai.destination payload — ids/roles only, never a URL or secret."""
        return {
            "id": self.destination.id,
            "priority": self.destination.priority,
            "role": self.destination.role,
            "provider": self.provider_type,
            "model": self.destination.model,
            "attempts": [
                {"destination_id": a.destination_id, "provider": a.provider,
                 "outcome": a.outcome, "reason": a.reason}
                for a in self.attempts
            ],
        }


class DestinationsExhausted(Exception):
    """Every destination failed with a retryable error; maps to the last one's status."""

    def __init__(
        self, attempts: tuple[DestinationAttempt, ...], last_error: ProviderError | None
    ) -> None:
        """Carry the attempt trail and the last retryable error for status mapping."""
        super().__init__("all destinations exhausted")
        self.attempts = attempts
        self.last_error = last_error

    def status_code(self) -> int:
        """429/504/502 from the last retryable error (default 502)."""
        if self.last_error is None:
            return 502
        return _STATUS_BY_REASON.get(classify_failure(self.last_error), 502)

    def retry_after(self) -> float | None:
        """The last attempt's Retry-After (seconds), iff that error was a rate limit."""
        if isinstance(self.last_error, ProviderRateLimitError):
            return self.last_error.retry_after
        return None


class FailoverDispatcher:
    """Ordered active/standby dispatch with breaker + first-byte semantics."""

    def __init__(
        self, registry: DestinationConnectorRegistry, breaker: DestinationBreaker,
        *, metrics: Any = None
    ) -> None:
        """Bind the connector registry, breaker, and optional metrics sink."""
        self._registry = registry
        self._breaker = breaker
        self._metrics = metrics

    async def dispatch(
        self, ctx: Any, destinations: list[Destination], messages: list
    ) -> Outcome:
        """Try destinations in order; return the first success or raise (spec §5.3)."""
        attempts: list[DestinationAttempt] = []
        last_error: ProviderError | None = None
        prev_provider: str | None = None

        for dest in destinations:
            if self._breaker.is_open(dest.id) and not self._breaker.reserve_probe(dest.id):
                attempts.append(DestinationAttempt(dest.id, dest.provider_type, "skipped", "breaker_open"))
                self._record(dest.provider_type, "skipped")
                continue
            try:
                connector = await self._registry.get(dest)
            except OwnershipError as exc:
                logger.error("failover: destination %s skipped (ownership): %s", dest.id, exc)
                attempts.append(DestinationAttempt(dest.id, dest.provider_type, "skipped", "ownership"))
                self._record(dest.provider_type, "skipped")
                continue
            try:
                text, usage, finish = await self._attempt(connector, dest, messages, ctx)
                self._breaker.record_success(dest.id)
                attempts.append(DestinationAttempt(dest.id, dest.provider_type, "ok", None))
                self._record(dest.provider_type, "ok")
                return Outcome(
                    destination=dest, provider_type=dest.provider_type, text=text,
                    usage=usage, finish_reason=finish, attempts=tuple(attempts),
                )
            except ProviderClientError:
                attempts.append(DestinationAttempt(dest.id, dest.provider_type, "client_error", "client_error"))
                self._record(dest.provider_type, "client_error")
                raise
            except ProviderError as exc:
                reason = classify_failure(exc)
                self._breaker.record_failure(dest.id)
                attempts.append(DestinationAttempt(dest.id, dest.provider_type, "failed", reason))
                self._record(dest.provider_type, "failed")
                if prev_provider is not None and self._metrics is not None:
                    self._metrics.record_destination_failover(prev_provider, dest.provider_type, reason)
                prev_provider = dest.provider_type
                if getattr(ctx, "bytes_flushed", False):
                    raise                                   # first-byte rule (§5.4)
                last_error = exc
                continue

        raise DestinationsExhausted(tuple(attempts), last_error)

    async def _attempt(
        self, connector: Any, dest: Destination, messages: list, ctx: Any
    ) -> tuple[str, dict, str]:
        """One bounded connector call; normalises timeouts/connection errors to ProviderError."""
        target_model = dest.provider_model_id or ctx.model
        timeout = dest.timeout_seconds
        try:
            if getattr(ctx, "stream", False):
                return await self._attempt_stream(connector, target_model, messages, timeout, dest)
            text, usage = await asyncio.wait_for(
                connector.chat_completion(messages, model=target_model), timeout=timeout
            )
            return text, usage, (usage or {}).get("finish_reason", "stop")
        except asyncio.TimeoutError as exc:
            raise ProviderTimeoutError(dest.provider_type, target_model, "attempt timeout") from exc
        except ProviderError:
            raise
        except Exception as exc:  # connection/transport error -> retryable server error
            raise ProviderServerError(
                dest.provider_type, target_model, f"connection error: {str(exc)[:100]}"
            ) from exc

    async def _attempt_stream(
        self, connector: Any, target_model: str, messages: list, timeout: int, dest: Destination
    ) -> tuple[str, dict, str]:
        """Bound time-to-first-chunk, then drain the stream, accumulating text + usage."""
        gen = connector.stream_chat_completion(messages, model=target_model)
        first = await asyncio.wait_for(gen.__anext__(), timeout=timeout)
        text = first.delta or ""
        usage = first.usage if first.done and first.usage else None
        async for chunk in gen:
            text += chunk.delta or ""
            if chunk.done and chunk.usage:
                usage = chunk.usage
        usage = usage or {}
        return text, usage, usage.get("finish_reason", "stop")

    def _record(self, provider_type: str, outcome: str) -> None:
        if self._metrics is not None:
            self._metrics.record_destination_attempt(provider_type, outcome)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/routing/test_failover_dispatcher.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/routing/failover.py tests/unit/routing/test_failover_dispatcher.py
git commit -m "feat(failover): FailoverDispatcher (retryable-only, breaker, first-byte, bounded)" \
  -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" \
  -m "Claude-Session: https://claude.ai/code/session_013GYHFHJZgh6t5u1v3hTb14"
```

---

### Task 11: Surface `clamp_local`/`provider_pin` + new `PipelineContext` fields (security-critical: S9)

**Files:**
- Modify: `shared/routing/engine.py` (`RouteDecision.clamp_local`; set it in `decide()`)
- Modify: `proxy/apps/proxy_server/pipeline/stages.py` (`PipelineContext` new fields; `RoutingStage` copies signals)
- Test: `tests/unit/routing/test_engine_clamp_local.py`, `tests/unit/proxy/test_routing_stage_failover.py`

**Interfaces:**
- Produces:
  - `RouteDecision.clamp_local: bool` (default `False`) — True when the sensitivity clamp OR budget-pressure `clamp_local` reshaped the chain (`engine.py:205-219`).
  - New `PipelineContext` fields (all default to the flag-off/no-op value so gated-off behaviour is byte-identical): `local_only: bool = False`, `provider_pin: str | None = None`, `bytes_flushed: bool = False`, `destination: dict | None = None`.
  - `RoutingStage` sets `ctx.local_only = decision.clamp_local` and `ctx.provider_pin = split_provider_prefix(ctx.requested_model)[0]` (the caller's hard `provider:model` pin), computed from the pre-routing requested model.
- Consumed by Task 8 (`resolve(pin=, local_only=)`), Task 14 (`DispatchStage` branch + `bytes_flushed` first-byte + `ctx.destination` marker).

**Rule reminder:** `ctx.preferred_backend` is a cache/affinity hint, **not** the pin — do not reuse it. The pin comes from `split_provider_prefix` on the *originally requested* model, captured before `RoutingStage` overwrites `ctx.model`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/routing/test_engine_clamp_local.py
from __future__ import annotations
from shared.routing.engine import RouteDecision


def test_route_decision_has_clamp_local_default_false():
    d = RouteDecision(model="gpt-4")
    assert d.clamp_local is False


def test_clamp_local_can_be_set():
    d = RouteDecision(model="ollama:llama3", clamp_local=True)
    assert d.clamp_local is True
```

```python
# tests/unit/proxy/test_routing_stage_failover.py
from __future__ import annotations
import pytest
from proxy.apps.proxy_server.pipeline.stages import PipelineContext, RoutingStage


def test_pipeline_context_new_fields_default_noop():
    ctx = PipelineContext(user=object(), body={})
    assert ctx.local_only is False
    assert ctx.provider_pin is None
    assert ctx.bytes_flushed is False
    assert ctx.destination is None


@pytest.mark.asyncio
async def test_routing_stage_copies_clamp_local_and_pin(monkeypatch):
    # A minimal RoutingStage whose engine returns a clamp_local decision and whose
    # requested model carries an ollama pin.
    from shared.routing.engine import RouteDecision

    class _Engine:
        async def decide(self, routing_input):
            return RouteDecision(model="llama3", fallback_chain=[], clamp_local=True)

    stage = RoutingStage.__new__(RoutingStage)
    stage.engine = _Engine()
    stage.rules = []
    stage.db = None
    stage.placement = None
    stage.backends_provider = None

    async def _offers(org_id=None):
        return []
    stage._load_offers = _offers  # type: ignore[assignment]

    ctx = PipelineContext(user=object(), body={"messages": []}, model="ollama:llama3")
    ctx.requested_model = "ollama:llama3"
    out = await stage(ctx)
    assert out.local_only is True
    assert out.provider_pin == "ollama"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/routing/test_engine_clamp_local.py tests/unit/proxy/test_routing_stage_failover.py -v --no-cov`
Expected: FAIL — `clamp_local`/new ctx fields not defined; `RoutingStage` doesn't set them.

- [ ] **Step 3: Write minimal implementation**

In `shared/routing/engine.py`, add the field to `RouteDecision`:
```python
    clamp_local: bool = False
```
In `decide()`, capture the clamp and pass it into the returned decision. The `clamp_local` variable already exists locally (`engine.py:212`); combine it with a PII-driven clamp and set it on the return:
```python
        chain = qualified
        pii_flagged = request.pii_detected
        clamp_local = pressure.clamp_local
        clamp_reshaped = False
        if pii_flagged or clamp_local:
            sensitivity_result = apply_sensitivity(
                chain,
                pii_detected=pii_flagged or clamp_local,
                org_sensitivity_routing=policy.sensitivity_routing if pii_flagged else "local_only",
            )
            chain = sensitivity_result.candidates
            clamp_reshaped = True
        ...
        return RouteDecision(
            model=final_model, fallback_chain=fallback_chain, routed_from=routed_from,
            trace=trace, clamp_local=clamp_reshaped,
        )
```
(The `clamp_reshaped` flag is True when either the budget-pressure clamp or the sensitivity clamp actually reshaped the chain — the exact condition the resolver's `local_only` needs, spec §5.1.)

In `proxy/apps/proxy_server/pipeline/stages.py`, add the four fields to `PipelineContext` (near `fallback_chain`/`routed_from`, each with a one-line comment):
```python
    # --- Provider-destination failover (failover spec §5) ---
    # True when RoutingStage's RouteDecision.clamp_local reshaped the chain
    # (sensitivity/budget clamp); restricts destinations to local providers.
    local_only: bool = False
    # The caller's hard `provider:model` pin (split_provider_prefix on the
    # originally requested model); restricts destinations to that provider.
    provider_pin: str | None = None
    # True once any byte has been flushed to the client — failover is illegal
    # after this (first-byte rule §5.4). Buffered dispatch keeps it False today.
    bytes_flushed: bool = False
    # The winning destination marker (usage.waddleai.destination §5.7), set by
    # the DispatchStage failover branch; None on the existing path.
    destination: dict | None = None
```
Import `split_provider_prefix` at the top of `stages.py` (`from shared.routing.aliases import split_provider_prefix`) and, in `RoutingStage.__call__`, after computing `decision` and before/after setting `ctx.model`, capture the pin from the requested model and copy the clamp:
```python
        pin_source = ctx.requested_model or ctx.model
        ctx.provider_pin = split_provider_prefix(pin_source)[0] if pin_source else None
        ctx.model = decision.model
        ctx.fallback_chain = decision.fallback_chain
        ctx.routed_from = decision.routed_from
        ctx.local_only = getattr(decision, "clamp_local", False)
        return ctx
```
> `ctx.requested_model` is the caller's original model. If a caller sets it before RoutingStage runs (as `main.py` does), the pin reflects the real request; if not, fall back to `ctx.model` (still pre-overwrite at this point in the stage). This matches the spec's "caller's `provider:model` hard pin".

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/routing/test_engine_clamp_local.py tests/unit/proxy/test_routing_stage_failover.py tests/unit/routing/test_engine.py tests/unit/proxy/test_routing_stage.py -v --no-cov`
Expected: PASS (new tests plus the existing engine + routing-stage suites — the new field defaults keep them green).

- [ ] **Step 5: Commit**

```bash
git add shared/routing/engine.py proxy/apps/proxy_server/pipeline/stages.py tests/unit/routing/test_engine_clamp_local.py tests/unit/proxy/test_routing_stage_failover.py
git commit -m "feat(failover): surface clamp_local/provider_pin + PipelineContext failover fields" \
  -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" \
  -m "Claude-Session: https://claude.ai/code/session_013GYHFHJZgh6t5u1v3hTb14"
```

---

### Task 12: Management REST routes — destinations + BYOK credentials (security-critical: S1, S2 write, S4 masking, S7 cap, IDOR)

**Files:**
- Create: `services/management/app/api/v1/routing_destinations.py`
- Modify: `services/management/app/api/v1/__init__.py` (append `routing_destinations,` to the import tuple)
- Test: `tests/unit/management/test_routing_destinations_routes.py`

**Interfaces:**
- Consumes: `require_auth`/`require_scope` (`auth.py`), `Permission.MODEL_DESTINATION_WRITE/DELETE` (Task 2), `is_feature_enabled` + `penguin_licensing` (gate pattern from `model_access_policies.py`), `encrypt_credential` (existing), `_mask_key` (existing pattern), `db` (`extensions`). Rides on `api_v1_bp` so quart-schema/OpenAPI pick the routes up (like `providers.py`).
- Produces the endpoints in spec §4:
  - `GET /api/v1/routing/destinations?model=` (auth) — this org's destinations, masked credential label only
  - `POST /api/v1/routing/destinations` (`MODEL_DESTINATION_WRITE`) — create; §3.2 invariants, ≤5 per model, provider enabled
  - `PATCH /api/v1/routing/destinations/<id>` (`MODEL_DESTINATION_WRITE`) — update priority/enabled/provider_model_id/region/timeout/credential
  - `DELETE /api/v1/routing/destinations/<id>` (`MODEL_DESTINATION_DELETE`)
  - `GET /api/v1/routing/destination-credentials` (auth) — this org's BYOK creds, `api_key_masked` only
  - `POST /api/v1/routing/destination-credentials` (`MODEL_DESTINATION_WRITE`) — create `provider_credentials` with `owner_org_id = org`, Fernet-encrypted, material validated by provider type
  - `DELETE /api/v1/routing/destination-credentials/<id>` (`MODEL_DESTINATION_DELETE`)
- Plus testable helpers: `_gate(org_id)`, `_resolve_org(g_user, requested_org)`, `_mask_material(provider_type, stored)`, `_validate_ownership(cred_row, dest_provider_id, org_id)`, `_count_enabled(org_id, model)`.

**Rule reminders:** `org_id = g.user.get("organization_id")` (S1). A body/query `organization_id` is honoured only with `Permission.PROVIDER_ADMIN`, else 403 on mismatch. A row addressed by id outside the resolved org → **404** (IDOR-safe, no existence leak). Ownership (S2 write): `credential.provider_id == destination.provider_id` AND (`credential.owner_org_id IS NULL` OR `== org_id`), else **422**. ≤5 enabled per `(org, model)` → **422** on the 6th. Masking is per provider type (S4): bearer → `_mask_key`; bedrock JSON → parse and mask only `aws_access_key_id`. Every response through a `@dataclass(slots=True)` DTO + `@validate_response`; plaintext material never returned/logged.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/management/test_routing_destinations_routes.py
from __future__ import annotations
import json
import pytest
from app.api.v1 import routing_destinations as rd


def test_mask_material_bearer():
    assert rd._mask_material("openai", "enc:sk-abcdefghij") == rd._mask_material("openai", "enc:sk-abcdefghij")
    masked = rd._mask_material("openai", "sk-abcdefghij")
    assert masked.startswith("sk-a") and masked.endswith("ghij") and "****" in masked


def test_mask_material_bedrock_masks_only_access_key_id():
    material = json.dumps({"aws_access_key_id": "AKIAEXAMPLE1234", "aws_secret_access_key": "supersecret"})
    masked = rd._mask_material("bedrock", material)
    assert "supersecret" not in masked                 # secret never leaks
    assert "aws_access_key_id" in masked
    assert "AKIA" in masked and "****" in masked        # only the access-key id is shown, masked


def test_resolve_org_rejects_cross_org_without_provider_admin():
    g_user = {"organization_id": 7, "scopes": []}
    org, err = rd._resolve_org(g_user, requested_org=99, has_provider_admin=False)
    assert org is None and err == 403


def test_resolve_org_allows_cross_org_with_provider_admin():
    g_user = {"organization_id": 7}
    org, err = rd._resolve_org(g_user, requested_org=99, has_provider_admin=True)
    assert org == 99 and err is None


def test_resolve_org_defaults_to_token_org():
    org, err = rd._resolve_org({"organization_id": 7}, requested_org=None, has_provider_admin=False)
    assert org == 7 and err is None


def test_validate_ownership_matrix():
    # same provider + platform credential (owner None) -> ok
    assert rd._validate_ownership({"provider_id": 3, "owner_org_id": None}, 3, 7) is None
    # same provider + same-org credential -> ok
    assert rd._validate_ownership({"provider_id": 3, "owner_org_id": 7}, 3, 7) is None
    # provider mismatch -> error
    assert rd._validate_ownership({"provider_id": 4, "owner_org_id": 7}, 3, 7) is not None
    # other-org credential -> error
    assert rd._validate_ownership({"provider_id": 3, "owner_org_id": 99}, 3, 7) is not None


@pytest.mark.asyncio
async def test_gate_flag_off_is_404(monkeypatch):
    monkeypatch.setenv("WADDLEAI_FLAG_PROVIDER_FAILOVER", "0")
    body, status = await rd._gate(7)
    assert status == 404


@pytest.mark.asyncio
async def test_gate_unentitled_is_403(monkeypatch):
    monkeypatch.setenv("WADDLEAI_FLAG_PROVIDER_FAILOVER", "1")
    monkeypatch.setattr(rd, "_get_license_client", lambda: type("L", (), {"check_feature": lambda self, f: False})())
    body, status = await rd._gate(7)
    assert status == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/management/test_routing_destinations_routes.py -v --no-cov`
Expected: FAIL — module/helpers not defined.

- [ ] **Step 3: Write minimal implementation**

`services/management/app/api/v1/routing_destinations.py` — gate modelled on `model_access_policies.py`; DTO + `@validate_response` modelled on `providers.py`; rides on `api_v1_bp`:
```python
"""WaddleAI Management API v1 — provider-destination + BYOK-credential CRUD (failover spec §4).

Two-layer Enterprise gate (``waddleai.provider_failover`` flag -> 404 when off;
``waddleai_provider_failover`` entitlement -> 403; fail-closed). Org from the validated
JWT only; cross-org requires PROVIDER_ADMIN (else 403 on mismatch). Rows addressed by id
outside the resolved org resolve to 404 (IDOR-safe). Ownership enforced at write (422);
<=5 enabled destinations per (org, model). Credential material is Fernet-encrypted and
never returned/logged — responses carry masked labels only (per provider type).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from quart import g, jsonify, request
from quart_schema import security_scheme, tag, validate_response

from shared.auth.rbac import Permission
from shared.utils.feature_flags import is_feature_enabled

from ...extensions import db
from . import api_v1_bp
from .auth import require_auth, require_scope
from .providers import _mask_key

logger = logging.getLogger(__name__)

_BEARER_AUTH = [{"bearerAuth": []}]
_FLAG_KEY = "waddleai.provider_failover"
_LICENSE_FEATURE = "waddleai_provider_failover"
MAX_DESTINATIONS_PER_MODEL = 5
_BEARER_TYPES = frozenset(
    {"openai", "anthropic", "gemini", "xai", "azure_openai", "cohere", "llamacpp"}
)
_license_client: Any = None


def _get_license_client() -> Any:
    """Lazily construct the shared penguin_licensing client (product must be 'waddleai')."""
    global _license_client
    if _license_client is None:
        from penguin_licensing import LicenseClient

        _license_client = LicenseClient(
            license_key=os.environ.get("LICENSE_KEY", ""),
            product="waddleai",
            base_url=os.environ.get("LICENSE_SERVER_URL", "https://license.penguintech.io"),
        )
    return _license_client


async def _gate(org_id: int | None) -> tuple | None:
    """404 when the flag is off, 403 when unentitled, else None. Fail-closed."""
    if not is_feature_enabled(_FLAG_KEY, distinct_id=str(org_id or "server"), default=False):
        return jsonify({"status": "error", "error": "not_found"}), 404

    def _check() -> bool:
        try:
            return bool(_get_license_client().check_feature(_LICENSE_FEATURE))
        except Exception as exc:  # pragma: no cover - defensive, license I/O failure
            logger.warning("routing_destinations: entitlement check failed: %s", exc)
            return False

    if not await asyncio.to_thread(_check):
        return (
            jsonify({"status": "error",
                     "error": "Provider failover requires an Enterprise entitlement "
                              "(waddleai_provider_failover)"}),
            403,
        )
    return None


def _resolve_org(
    g_user: dict, requested_org: int | None, has_provider_admin: bool
) -> tuple[int | None, int | None]:
    """Resolve the effective org; cross-org requires PROVIDER_ADMIN (else 403). Returns (org, err)."""
    token_org = g_user.get("organization_id")
    if requested_org is None or int(requested_org) == int(token_org):
        return int(token_org), None
    if has_provider_admin:
        return int(requested_org), None
    return None, 403


def _mask_material(provider_type: str, stored: str | None) -> str:
    """Mask credential material per provider type — bearer via _mask_key, bedrock only the access-key id."""
    if not stored:
        return ""
    if provider_type == "bedrock":
        raw = stored[4:] if stored.startswith("enc:") else stored
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return "****"
        akid = parsed.get("aws_access_key_id") or ""
        return f"aws_access_key_id={_mask_key(akid)}" if akid else "****"
    return _mask_key(stored)


def _validate_ownership(cred_row: Any, dest_provider_id: int, org_id: int) -> str | None:
    """S2 write invariant: same provider AND (platform OR same-org) credential; else an error message."""
    cred_provider = cred_row["provider_id"] if isinstance(cred_row, dict) else cred_row.provider_id
    owner = cred_row["owner_org_id"] if isinstance(cred_row, dict) else cred_row.owner_org_id
    if cred_provider != dest_provider_id:
        return "credential.provider_id must match the destination's provider_id"
    if owner is not None and int(owner) != int(org_id):
        return "credential is owned by another org"
    return None


async def _count_enabled(org_id: int, model: str) -> int:
    """Count enabled destinations for (org, model) — for the <=5 cap (S7)."""
    def _fetch() -> int:
        return db(
            (db.model_destinations.organization_id == org_id)
            & (db.model_destinations.model == model)
            & (db.model_destinations.enabled == True)  # noqa: E712
        ).count()
    return await asyncio.to_thread(_fetch)
```
Add the DTOs and route handlers below the helpers (all responses through a DTO + `@validate_response`). Destination masks the credential to a label only; credentials mask material per type:
```python
@dataclass(slots=True)
class DestinationDTO:
    """One destination row for API responses — never a credential secret."""
    id: int
    organization_id: int
    model: str
    priority: int
    provider_id: int
    credential_id: int | None
    credential_label: str | None
    provider_model_id: str | None
    region: str | None
    timeout_seconds: int | None
    enabled: bool


@dataclass(slots=True)
class DestinationListResponse:
    """GET /routing/destinations body."""
    destinations: list[DestinationDTO]
    total: int


@dataclass(slots=True)
class DestinationCredentialDTO:
    """A BYOK provider_credentials row — api_key_masked only, never plaintext."""
    id: int
    provider_id: int
    label: str
    api_key_masked: str
    owner_org_id: int | None
    enabled: bool


@dataclass(slots=True)
class DestinationCredentialListResponse:
    """GET /routing/destination-credentials body."""
    credentials: list[DestinationCredentialDTO]
    total: int
```
Implement the seven handlers using these helpers. Each handler: resolve org (S1), call `_gate` (404/403), then the org-scoped query (IDOR → 404). Create/patch destinations validate `_validate_ownership` (422), `_count_enabled` (422 on the 6th new-enabled), and that the provider is enabled. Credential create validates material by provider type (bedrock: JSON with `aws_access_key_id`+`aws_secret_access_key`; bearer types: non-empty string), then `db.provider_credentials.insert(..., owner_org_id=org_id, api_key=encrypt_credential(material))`. Credential delete is org-scoped (`owner_org_id == org_id`) → destinations referencing it get `credential_id=NULL` via the FK `ON DELETE SET NULL`. Full handler bodies mirror `create_access_policy`/`update_access_policy` in `model_access_policies.py` (org-scoped `_fetch`/`_create`/`_update` under `asyncio.to_thread`, `db.commit()`), differing only in table + validation. Keep the module under 25,000 chars.

Append `routing_destinations,` to the import tuple in `services/management/app/api/v1/__init__.py` (append at the end, per that file's append-only comment — do not reorder).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/management/test_routing_destinations_routes.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/api/v1/routing_destinations.py services/management/app/api/v1/__init__.py tests/unit/management/test_routing_destinations_routes.py
git commit -m "feat(failover): destination + BYOK-credential management routes (gated, IDOR-safe)" \
  -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" \
  -m "Claude-Session: https://claude.ai/code/session_013GYHFHJZgh6t5u1v3hTb14"
```

---

### Task 13: Platform credential endpoints exclude tenant-owned rows (security-critical: S12)

**Files:**
- Modify: `services/management/app/api/v1/providers.py` (add `owner_org_id IS NULL` filter to the `/providers/<id>/credentials` list, update/PATCH, and delete surfaces)
- Test: `tests/unit/management/test_providers_credentials_tenant_filter.py`

**Interfaces:**
- Produces: the existing platform credential endpoints (`list_provider_credentials`, `update_provider_credential`, `delete_provider_credential`) now filter `owner_org_id IS NULL`, so a platform provider-admin can never list, rotate, or delete a tenant's BYOK key through that surface (S12). BYOK rows are reachable only via Task 12's routes.

**Rule reminder:** reference `owner_org_id`, never the provider workspace `org_id`. A BYOK row addressed by id through these endpoints must resolve to **404** (not found), not leak its existence.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/management/test_providers_credentials_tenant_filter.py
from __future__ import annotations
import inspect
from app.api.v1 import providers


def test_list_credentials_query_filters_owner_org_id_null():
    src = inspect.getsource(providers.list_provider_credentials)
    assert "owner_org_id" in src, "platform credential list must exclude tenant-owned rows (S12)"


def test_update_credentials_scoped_to_platform_rows():
    src = inspect.getsource(providers.update_provider_credential)
    assert "owner_org_id" in src


def test_delete_or_rotate_scoped_to_platform_rows():
    # delete is a soft/hard op depending on the endpoint; the existence check must be
    # constrained to platform rows so a BYOK id resolves to 404, not a mutation.
    for fn_name in ("delete_provider_credential",):
        if hasattr(providers, fn_name):
            assert "owner_org_id" in inspect.getsource(getattr(providers, fn_name))
```

> If `delete_provider_credential` does not yet exist as a separate handler (rotation/disable may live inside `update_provider_credential`), the third test's loop simply skips it; the list + update guards are the load-bearing ones. When wiring, confirm every `db(...provider_credentials...)` query on this surface carries the filter.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/management/test_providers_credentials_tenant_filter.py -v --no-cov`
Expected: FAIL — the platform credential queries don't reference `owner_org_id`.

- [ ] **Step 3: Write minimal implementation**

In `providers.py`, add `& (db.provider_credentials.owner_org_id == None)  # noqa: E711 -- platform pool only (S12)` to every `db(...)` query on `provider_credentials` in `list_provider_credentials` (the `_fetch` inner query), `update_provider_credential` (the `_check_existence` and `_update` inner queries), and any create/delete/rotate helper on this surface. Example (list):
```python
        return db(
            (db.provider_credentials.provider_id == provider_id)
            & (db.provider_credentials.owner_org_id == None)  # noqa: E711 -- platform pool only (S12)
        ).select(orderby=db.provider_credentials.id)
```
For `update_provider_credential`, the credential existence lookup becomes:
```python
        cred = (
            db(
                (db.provider_credentials.id == cred_id)
                & (db.provider_credentials.provider_id == provider_id)
                & (db.provider_credentials.owner_org_id == None)  # noqa: E711 -- S12
            )
            .select()
            .first()
        )
```
so a BYOK `cred_id` returns `cred_not_found` → 404.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/management/test_providers_credentials_tenant_filter.py tests/unit/management/test_providers.py -v --no-cov`
Expected: PASS (guard tests plus the existing providers suite, which uses only platform rows).

- [ ] **Step 5: Commit**

```bash
git add services/management/app/api/v1/providers.py tests/unit/management/test_providers_credentials_tenant_filter.py
git commit -m "fix(failover): platform credential endpoints exclude tenant-owned rows (S12)" \
  -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" \
  -m "Claude-Session: https://claude.ai/code/session_013GYHFHJZgh6t5u1v3hTb14"
```

---

### Task 14: `DispatchStage` failover branch + `main.py` marker + stats (security-critical: S6, S10, S11)

**Files:**
- Modify: `proxy/apps/proxy_server/pipeline/stages.py` (`DispatchStage.__init__` gains optional failover collaborators; a new branch before `select_provider`)
- Modify: `proxy/apps/proxy_server/main.py` (wire resolver/registry/dispatcher/gate in `startup()`; merge `usage.waddleai.destination` in both builders; add `destinations` to `/api/routing/stats`)
- Test: `tests/unit/proxy/test_dispatch_stage_failover.py`

**Interfaces:**
- Consumes: `FailoverGate` (Task 6), `DestinationResolver` (Task 8), `DestinationConnectorRegistry` (Task 9), `FailoverDispatcher`/`Outcome`/`DestinationsExhausted` (Task 10), `ctx.local_only`/`ctx.provider_pin`/`ctx.bytes_flushed`/`ctx.destination` (Task 11), the `WaddleAIMetrics` gate/attempt methods (Task 5).
- Produces: `DispatchStage.__init__` new optional params `failover_gate`, `destination_resolver`, `failover_dispatcher`, `metrics` (all default `None`); when any is `None` the failover branch is inert and the existing path runs **byte-for-byte unchanged** (S10). `main.py` merges the destination marker and exposes breaker state. On a 429 `DestinationsExhausted` with a non-None `retry_after()`, `ctx.usage_meta["retry_after"]` is set and both error-response builders emit a `Retry-After: <int seconds>` header.

**Rule reminders (spec §5.1):** org from `ctx.user` (`tenant_id` or `organization_id`), never the request body (S1). Gate first — flag OFF / not entitled → count `record_destination_gate_denied(reason)` and fall through (S10). Apply the security_v2 upstream filter identically to the existing branch (pseudonymise before dispatch, de-pseudonymise + cleanup after) (S11). Populate `ctx.provider/requested_model/model/response_text/usage/finish_reason` exactly as the existing path so `MeterStage` counts correctly. Failover only before the first flushed byte (`ctx.bytes_flushed`, S6 — enforced inside the dispatcher).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/proxy/test_dispatch_stage_failover.py
from __future__ import annotations
from types import SimpleNamespace
import pytest
from proxy.apps.proxy_server.pipeline.stages import DispatchStage, PipelineContext
from shared.routing.destinations import Destination
from shared.routing.failover import DestinationAttempt, DestinationsExhausted, Outcome
from shared.utils.llm_connectors import ProviderRateLimitError


def _dest():
    return Destination(id=1, organization_id=7, model="claude-sonnet-4", priority=0, provider_id=3,
                       provider_type="bedrock", endpoint_url=None, region="us-west-2",
                       provider_model_id="anthropic.claude-sonnet-4-v1:0", timeout_seconds=30,
                       credential_id=5, owner_org_id=7, credential_version="v1")


class _Gate:
    def __init__(self, enabled, reason="ok"): self._e, self._r = enabled, reason
    async def evaluate(self, org_id): return (self._e, self._r)


class _Resolver:
    def __init__(self, dests): self.dests = dests; self.calls = []
    async def resolve(self, org_id, model, *, pin=None, local_only=False):
        self.calls.append((org_id, model, pin, local_only)); return self.dests


class _Dispatcher:
    def __init__(self, outcome=None, exc=None): self._o, self._exc = outcome, exc; self.messages = None
    async def dispatch(self, ctx, dests, messages):
        self.messages = messages
        if self._exc:
            raise self._exc
        return self._o


class _Metrics:
    def __init__(self): self.denied = []
    def record_destination_gate_denied(self, reason): self.denied.append(reason)


def _stage(gate, resolver, dispatcher, metrics=None):
    return DispatchStage(
        name="dispatch", router=SimpleNamespace(select_provider=lambda *a, **k: None),
        connectors={}, failover_gate=gate, destination_resolver=resolver,
        failover_dispatcher=dispatcher, metrics=metrics,
    )


def _ctx():
    ctx = PipelineContext(user=SimpleNamespace(tenant_id=7), body={}, model="claude-sonnet-4",
                          messages=[{"role": "user", "content": "hi"}])
    ctx.requested_model = "claude-sonnet-4"
    return ctx


@pytest.mark.asyncio
async def test_failover_branch_populates_ctx_and_marker():
    outcome = Outcome(destination=_dest(), provider_type="bedrock", text="answer",
                      usage={"input_tokens": 3, "output_tokens": 4, "finish_reason": "stop"},
                      finish_reason="stop",
                      attempts=(DestinationAttempt(1, "bedrock", "ok", None),))
    stage = _stage(_Gate(True), _Resolver([_dest()]), _Dispatcher(outcome=outcome))
    out = await stage(_ctx())
    assert out.blocked is False
    assert out.provider == "bedrock"
    assert out.requested_model == "claude-sonnet-4"
    assert out.model == "anthropic.claude-sonnet-4-v1:0"     # provider_model_id
    assert out.response_text == "answer"
    assert out.usage["output_tokens"] == 4
    assert out.destination["role"] == "active"


@pytest.mark.asyncio
async def test_gate_off_skips_resolve_and_counts_denied():
    resolver = _Resolver([_dest()])
    metrics = _Metrics()
    stage = _stage(_Gate(False, "flag_off"), resolver, _Dispatcher(), metrics=metrics)
    # Existing path has no connectors -> it will block with no_available_providers,
    # but the point is: no resolve() call, and the denial is counted (S10).
    out = await stage(_ctx())
    assert resolver.calls == []                               # no new SQL on the hot path
    assert metrics.denied == ["flag_off"]


@pytest.mark.asyncio
async def test_resolver_receives_pin_and_local_only():
    resolver = _Resolver([])   # empty -> falls through to existing path
    ctx = _ctx(); ctx.provider_pin = "bedrock"; ctx.local_only = True
    stage = _stage(_Gate(True), resolver, _Dispatcher())
    await stage(ctx)
    assert resolver.calls[0] == (7, "claude-sonnet-4", "bedrock", True)


@pytest.mark.asyncio
async def test_destinations_exhausted_maps_to_status():
    exc = DestinationsExhausted((), None)
    stage = _stage(_Gate(True), _Resolver([_dest()]), _Dispatcher(exc=exc))
    out = await stage(_ctx())
    assert out.blocked is True and out.status_code == 502
    assert out.block_reason == "destinations_exhausted"


@pytest.mark.asyncio
async def test_destinations_exhausted_429_sets_usage_meta_retry_after():
    exc = DestinationsExhausted(
        (), ProviderRateLimitError("openai", "m", "429", status_code=429, retry_after=5.0)
    )
    stage = _stage(_Gate(True), _Resolver([_dest()]), _Dispatcher(exc=exc))
    out = await stage(_ctx())
    assert out.blocked is True and out.status_code == 429
    assert out.usage_meta["retry_after"] == 5.0


@pytest.mark.asyncio
async def test_no_failover_collaborators_is_existing_path():
    # gate/resolver/dispatcher all None -> failover branch inert (byte-for-byte, S10)
    stage = DispatchStage(name="dispatch",
                          router=SimpleNamespace(select_provider=lambda *a, **k: None),
                          connectors={})
    out = await stage(_ctx())
    assert out.blocked is True and out.block_reason == "no_available_providers"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/proxy/test_dispatch_stage_failover.py -v --no-cov`
Expected: FAIL — `DispatchStage` has no failover params/branch.

- [ ] **Step 3: Write minimal implementation**

Extend `DispatchStage.__init__` with the optional collaborators (keep every existing param):
```python
        failover_gate: Any = None,
        destination_resolver: Any = None,
        failover_dispatcher: Any = None,
        metrics: Any = None,
```
storing each on `self`. At the very top of `__call__`, after the `ctx.cache_hit` / empty-messages guards and before the `select_provider` block, insert the branch:
```python
        if await self._maybe_failover(ctx):
            return ctx
```
Add the method (populates ctx exactly as the existing path, then returns True; returns False to fall through):
```python
    async def _maybe_failover(self, ctx: PipelineContext) -> bool:
        """Try the destination-failover path; return True if it handled the request (spec §5.1)."""
        if self.failover_dispatcher is None or self.failover_gate is None or self.destination_resolver is None:
            return False
        org_raw = getattr(ctx.user, "tenant_id", None) or getattr(ctx.user, "organization_id", None)
        try:
            org_id = int(org_raw) if org_raw is not None else 0
        except (TypeError, ValueError):
            org_id = 0

        enabled, reason = await self.failover_gate.evaluate(org_id)
        if not enabled:
            if self.metrics is not None:
                self.metrics.record_destination_gate_denied(reason)
            return False

        dests = await self.destination_resolver.resolve(
            org_id, ctx.model, pin=ctx.provider_pin, local_only=ctx.local_only
        )
        if not dests:
            return False

        provider_hint = dests[0].provider_type
        if self.upstream_filter is not None and self.features is not None and self.features.is_feature_enabled(
            "waddleai.security_v2", distinct_id=str(org_id)
        ):
            await self._apply_upstream_filter(ctx, org_id, provider_hint, ctx.model)

        requested = ctx.model
        try:
            outcome = await self.failover_dispatcher.dispatch(ctx, dests, ctx.messages)
        except ProviderClientError as e:
            ctx.blocked = True
            ctx.status_code = e.status_code or 400
            ctx.block_reason = f"provider_error_{e.status_code}"
            return True
        except DestinationsExhausted as e:
            ctx.blocked = True
            ctx.status_code = e.status_code()
            ctx.block_reason = "destinations_exhausted"
            retry_after = e.retry_after()
            if ctx.status_code == 429 and retry_after is not None:
                ctx.usage_meta["retry_after"] = retry_after
            return True
        except Exception as e:  # pragma: no cover - defensive
            logger.error("DispatchStage: failover dispatch error: %s", e, exc_info=True)
            ctx.blocked = True
            ctx.status_code = 500
            ctx.block_reason = "dispatch_error"
            return True

        ctx.provider = outcome.provider_type
        ctx.requested_model = requested
        ctx.model = outcome.destination.provider_model_id or requested
        ctx.response_text = outcome.text
        ctx.usage = outcome.usage
        ctx.finish_reason = outcome.finish_reason
        if ctx.upstream_mapping_id and self.upstream_filter is not None:
            ctx.response_text = await self.upstream_filter.depseudonymize(
                ctx.response_text, ctx.upstream_mapping_id
            )
            await self.upstream_filter.cleanup(ctx.upstream_mapping_id)
            ctx.upstream_mapping_id = None
        ctx.destination = outcome.marker
        return True
```
Import `DestinationsExhausted` at the top of `stages.py` (`from shared.routing.failover import DestinationsExhausted`). `ProviderClientError` is already imported.

In `proxy/apps/proxy_server/main.py`:
1. In `startup()`, construct the collaborators once and pass them to `DispatchStage`:
```python
        from shared.routing.failover_gate import FailoverGate
        from shared.routing.destinations import DestinationResolver
        from shared.routing.destination_connectors import DestinationConnectorRegistry
        from shared.routing.destination_breaker import DestinationBreaker
        from shared.routing.failover import FailoverDispatcher

        self.destination_resolver = DestinationResolver(self.db)
        self.destination_registry = DestinationConnectorRegistry(self.destination_resolver.load_material)
        self.destination_breaker = DestinationBreaker()
        self.failover_dispatcher = FailoverDispatcher(
            self.destination_registry, self.destination_breaker, metrics=self.metrics
        )
        self.failover_gate = FailoverGate()
```
and wherever `DispatchStage(...)` is built, pass `failover_gate=self.failover_gate, destination_resolver=self.destination_resolver, failover_dispatcher=self.failover_dispatcher, metrics=self.metrics`.
2. In both response builders, merge the destination marker after `routing_meta` (OpenAI `main.py:1477-1482`; Anthropic `/v1/messages` `:1797-1802`):
```python
        destination_meta = {"destination": ctx.destination} if ctx.destination else None
        waddleai_usage = _merge_waddleai_usage(waddleai_usage, destination_meta)
```
(placed right after the existing `waddleai_usage = _merge_waddleai_usage(...)` line, before the `if waddleai_usage is not None:` guard).
3. In `/api/routing/stats` (`main.py:1523`), add the breaker snapshot:
```python
                "destinations": proxy_server.destination_breaker.snapshot(),
```
to the returned dict (guard with `getattr(proxy_server, "destination_breaker", None)` so the endpoint still works if failover was never constructed).
4. In both `if ctx.blocked:` error-response blocks (OpenAI `main.py:1383-1386`; Anthropic `/v1/messages` `:1715-1720`), emit a `Retry-After` header when `ctx.usage_meta` carries one (set by the `DestinationsExhausted` 429 branch above — absent on every other block reason, so this is a no-op on the existing path):
```python
        if ctx.blocked:
            status_code = ctx.status_code or 500
            error_msg = ctx.block_reason or "Request blocked"
            response = jsonify({"error": {"message": error_msg, "type": "error"}})
            retry_after = ctx.usage_meta.get("retry_after") if status_code == 429 else None
            if retry_after is not None:
                response.headers["Retry-After"] = str(int(retry_after))
            return response, status_code
```
(same pattern for the Anthropic block — keep its existing `{"error": {"type": "invalid_request_error", "message": error_msg}}` body shape, just wrap it in `response = jsonify(...)` and add the same `retry_after`/header logic before `return response, status_code`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/proxy/test_dispatch_stage_failover.py tests/unit/proxy/test_pipeline_stages.py tests/unit/proxy/test_pipeline.py -v --no-cov`
Expected: PASS (new failover tests plus the existing DispatchStage/pipeline suites — the branch is inert when collaborators are absent, so the existing path is byte-for-byte unchanged).

- [ ] **Step 5: Commit**

```bash
git add proxy/apps/proxy_server/pipeline/stages.py proxy/apps/proxy_server/main.py tests/unit/proxy/test_dispatch_stage_failover.py
git commit -m "feat(failover): DispatchStage failover branch + usage.waddleai.destination + stats" \
  -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" \
  -m "Claude-Session: https://claude.ai/code/session_013GYHFHJZgh6t5u1v3hTb14"
```

---

### Task 15: OpenAPI regeneration + docs + release notes

**Files:**
- Modify (generated): `openapi/v1.yaml`
- Modify: `docs/docs-site/docs/architecture.md`, `docs/docs-site/docs/api/openai-compatible.md`, `docs/docs-site/docs/api/management-api.md`
- Create: `docs/docs-site/docs/routing/destination-failover.md`
- Modify: `docs/RELEASE_NOTES.md`

**Interfaces:**
- Consumes: the routes registered in Task 12 (the OpenAPI generator reads the `quart-schema` annotations on `api_v1_bp`).
- Produces: a committed `openapi/v1.yaml` containing the seven destination/credential endpoints; four docs pages describing the feature; an Unreleased release-note entry. CI hard-fails on OpenAPI drift, so this must run after Tasks 12/13.

**Rule reminder:** `make generate-openapi` writes the spec; never hand-edit `openapi/v1.yaml`. `make openapi-lint` (spectral) gates on error. The management-API docs/spec routes stay behind auth — no change to the two-document split.

- [ ] **Step 1: Regenerate the OpenAPI spec and confirm the new paths appear**

Run: `make generate-openapi`
Then confirm the destination paths were emitted:
Run: `grep -c "/routing/destinations" openapi/v1.yaml`
Expected: a non-zero count (≥1). If zero, Task 12's routes are not registered on `api_v1_bp` or the module was not appended to `api/v1/__init__.py` — fix there before continuing.

- [ ] **Step 2: Lint the spec**

Run: `make openapi-lint`
Expected: PASS (no errors).

- [ ] **Step 3: Write the docs**

Create `docs/docs-site/docs/routing/destination-failover.md` — the Bedrock→Anthropic walkthrough:
```markdown
# Provider Destination Failover

Serve one logical model from your own destinations in priority order, each with its
own credential. Example: point `claude-sonnet-4` at your AWS Bedrock account first,
and fall back to your Anthropic Team key when Bedrock is throttled or down.

## How it works

- A **destination** is `(provider, credential, provider-specific model id, region, timeout)`.
- Destinations are ordered by `priority`: `0` = active, `≥1` = standby (tried ascending).
- Failover is **implicit** when two or more enabled destinations exist for one model.
- Only **retryable** failures fail over: timeouts, connection errors, HTTP 429
  (incl. Bedrock `ThrottlingException`), and 5xx (incl. Anthropic 529/503). A 4xx
  (a bad key, a bad request) is **surfaced to you**, never failed over.
- A per-destination circuit breaker trips after 3 consecutive failures and holds the
  destination out for 60 s (one half-open probe on recovery).
- At most **5** enabled destinations per model; each attempt is bounded by
  `timeout_seconds` (default 30).

## Tenant-owned (BYOK) credentials

Credentials you create here are **owned by your org** and are used **only** by your
destinations — never by the platform pool or another org. Bedrock credential material
is a JSON object `{"aws_access_key_id","aws_secret_access_key","aws_session_token"?}`;
an empty Bedrock credential uses the ambient AWS chain / IAM role. Bearer providers
(OpenAI, Anthropic, …) take the key string.

## Walkthrough: Bedrock active, Anthropic standby

1. Create a BYOK credential for your Bedrock account
   (`POST /api/v1/routing/destination-credentials`, provider = your Bedrock provider).
2. Create a BYOK credential for your Anthropic Team key.
3. Create the active destination (`POST /api/v1/routing/destinations`, `priority: 0`,
   `provider_model_id: "anthropic.claude-sonnet-4-v1:0"`, `region: "us-west-2"`).
4. Create the standby (`priority: 1`, the Anthropic provider + credential,
   `provider_model_id: "claude-sonnet-4-...")`.
5. Call `claude-sonnet-4` as usual. When Bedrock is throttled, the response is served
   from Anthropic using your Team key, and `usage.waddleai.destination` records which
   destination served it and every attempt made.

## Enablement

Enterprise-tier, behind the `waddleai.provider_failover` flag. When the flag is off or
the entitlement is absent, requests take the existing single-destination path unchanged.
```
Add a short failover box to `docs/docs-site/docs/architecture.md` (near the routing/dispatch section) linking to the new page. Add a note to `docs/docs-site/docs/api/openai-compatible.md` that a `provider:model` pin (e.g. `ollama:llama3`) restricts failover to that provider. Add the seven endpoint rows to `docs/docs-site/docs/api/management-api.md` (method, path, scope, one-line behaviour), matching the table in spec §4.

- [ ] **Step 4: Write the release note**

Prepend under the existing `## Unreleased` heading in `docs/RELEASE_NOTES.md` a new subsection (above `### Security`):
```markdown
### Added

- **Provider destination failover (Enterprise, flag-gated).** An org can now serve one
  logical model from ordered active/standby destinations, each with its own tenant-owned
  (BYOK) credential — e.g. AWS Bedrock active, Anthropic Team key standby. Only retryable
  failures (timeout / 429 / 5xx) fail over; 4xx is surfaced. A per-destination circuit
  breaker (3 failures / 60 s) and a ≤5-destination cap bound worst-case latency. New
  management routes under `/api/v1/routing/destinations` and
  `/api/v1/routing/destination-credentials`; `usage.waddleai.destination` reports which
  destination served each request. Behind the `waddleai.provider_failover` flag +
  `waddleai_provider_failover` entitlement; when off, behaviour is unchanged.
```

- [ ] **Step 5: Verify and commit**

Run: `make openapi-lint`
Expected: PASS.

```bash
git add openapi/v1.yaml docs/docs-site/docs/routing/destination-failover.md docs/docs-site/docs/architecture.md docs/docs-site/docs/api/openai-compatible.md docs/docs-site/docs/api/management-api.md docs/RELEASE_NOTES.md
git commit -m "docs(failover): OpenAPI spec + destination-failover docs + release note" \
  -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" \
  -m "Claude-Session: https://claude.ai/code/session_013GYHFHJZgh6t5u1v3hTb14"
```

---

### Task 16: In-process two-stub failover harness + scenarios (security-critical: S2 distinct-key, S5, S6)

**Files:**
- Create: `tests/unit/failover/__init__.py`, `tests/unit/failover/conftest.py`, `tests/unit/failover/test_two_stub_failover.py`

**Interfaces:**
- Consumes: the **real** `DestinationResolver` (Task 8), `DestinationConnectorRegistry` (Task 9), `FailoverDispatcher` + `DestinationBreaker` (Tasks 10/4), and the real `OpenAIConnector` (Task 3) — driven end-to-end, not through HTTP to the proxy.
- Produces: two in-process `aiohttp` OpenAI-compatible stub servers on ephemeral ports (each a `/v1/chat/completions` endpoint, configurable to return 200 / 429+`Retry-After` / 503 / hang / 401, recording the `Authorization` header it received), wired as two `ai_providers` rows of type `openai` with **distinct BYOK credentials** in an in-memory `FakeDB` whose `executesql` returns the joined destination rows and the per-credential material.

**Placement decision (stated): these live in `tests/unit/failover/`, NOT under `pytest.mark.integration`.** The stubs bind `127.0.0.1:0` (an ephemeral loopback port) via `aiohttp` (a pinned dependency, `aiohttp==3.14.3`) and are torn down in-fixture — there is **no external service, container, or network egress**, so the suite is deterministic and self-contained. It therefore belongs in the unit tree and **counts toward the 90% coverage gate** (exercising the real resolver→registry→dispatcher→connector path end-to-end). The `integration` marker is reserved for tests that "require live services" (`pytest.ini`), which this is not.

- [ ] **Step 1: Write the harness fixtures**

`tests/unit/failover/conftest.py`:
```python
"""In-process OpenAI-compatible stub servers + a FakeDB, for end-to-end failover tests.

Fully in-process (aiohttp on 127.0.0.1:0, no external service) — belongs in the unit
tree and counts toward coverage. Each stub records the Authorization header it saw so a
test can assert the standby used its OWN distinct key (S2).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest_asyncio
from aiohttp import web


class StubProvider:
    """A configurable OpenAI-compatible /v1/chat/completions stub."""

    def __init__(self, *, mode: str = "ok", text: str = "hi", retry_after: str | None = None):
        """mode in ok|rate_limit|server_error|hang|unauthorized; records the last auth header seen."""
        self.mode = mode
        self.text = text
        self.retry_after = retry_after
        self.seen_auth: list[str | None] = []
        self._runner: web.AppRunner | None = None
        self.port: int = 0

    async def _handler(self, request: web.Request) -> web.Response:
        self.seen_auth.append(request.headers.get("Authorization"))
        if self.mode == "hang":
            await asyncio.sleep(10)
        if self.mode == "rate_limit":
            headers = {"Retry-After": self.retry_after} if self.retry_after else {}
            return web.json_response({"error": "rate limited"}, status=429, headers=headers)
        if self.mode == "server_error":
            return web.json_response({"error": "unavailable"}, status=503)
        if self.mode == "unauthorized":
            return web.json_response({"error": "bad key"}, status=401)
        return web.json_response({
            "id": "chatcmpl-x", "object": "chat.completion", "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": self.text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        })

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self._handler)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = self._runner.addresses[0][1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


class FakeDB:
    """executesql over two openai destinations with distinct BYOK credentials."""

    def __init__(self, active: StubProvider, standby: StubProvider,
                 active_key: str, standby_key: str):
        self._rows = [
            (1, 7, "gpt-4", 0, 101, "openai", active.base_url, None, None, None, 5, 501, 7, "v1"),
            (2, 7, "gpt-4", 1, 102, "openai", standby.base_url, None, None, None, 5, 502, 7, "v1"),
        ]
        self._material = {
            501: (501, 101, 7, active_key, "v1"),
            502: (502, 102, 7, standby_key, "v1"),
        }

    def executesql(self, sql: str, params: Any = None):
        s = sql.strip().upper()
        if "FROM MODEL_DESTINATIONS" in s:
            return list(self._rows)
        if "FROM PROVIDER_CREDENTIALS" in s:
            return [self._material[params[0]]] if params and params[0] in self._material else []
        return []


@pytest_asyncio.fixture
async def two_stubs():
    """Yield (active, standby) started stubs; caller sets .mode; torn down after."""
    active, standby = StubProvider(text="from-active"), StubProvider(text="from-standby")
    await active.start(); await standby.start()
    yield active, standby
    await active.stop(); await standby.stop()
```

- [ ] **Step 2: Write the failing scenario tests**

`tests/unit/failover/test_two_stub_failover.py`:
```python
from __future__ import annotations
from types import SimpleNamespace
import pytest
from tests.unit.failover.conftest import FakeDB
from shared.routing.destinations import DestinationResolver
from shared.routing.destination_connectors import DestinationConnectorRegistry
from shared.routing.destination_breaker import DestinationBreaker
from shared.routing.failover import DestinationsExhausted, FailoverDispatcher
from shared.utils.llm_connectors import ProviderClientError

ACTIVE_KEY, STANDBY_KEY = "sk-active-0001", "sk-standby-0002"


def _stack(db):
    resolver = DestinationResolver(db, ttl_seconds=0.0)
    registry = DestinationConnectorRegistry(resolver.load_material)
    return resolver, FailoverDispatcher(registry, DestinationBreaker())


def _ctx():
    return SimpleNamespace(model="gpt-4", stream=False, bytes_flushed=False)


@pytest.mark.asyncio
async def test_active_503_fails_over_and_standby_uses_its_own_key(two_stubs):
    active, standby = two_stubs
    active.mode = "server_error"
    db = FakeDB(active, standby, ACTIVE_KEY, STANDBY_KEY)
    resolver, dispatcher = _stack(db)
    dests = await resolver.resolve(7, "gpt-4")
    out = await dispatcher.dispatch(_ctx(), dests, [{"role": "user", "content": "hi"}])
    assert out.text == "from-standby"
    assert standby.seen_auth[-1] == f"Bearer {STANDBY_KEY}"     # S2 — standby used ITS key
    assert active.seen_auth[-1] == f"Bearer {ACTIVE_KEY}"       # active used its own


@pytest.mark.asyncio
async def test_active_429_with_retry_after_fails_over(two_stubs):
    active, standby = two_stubs
    active.mode = "rate_limit"; active.retry_after = "3"
    db = FakeDB(active, standby, ACTIVE_KEY, STANDBY_KEY)
    resolver, dispatcher = _stack(db)
    out = await dispatcher.dispatch(_ctx(), await resolver.resolve(7, "gpt-4"),
                                    [{"role": "user", "content": "hi"}])
    assert out.text == "from-standby"


@pytest.mark.asyncio
async def test_active_hang_past_timeout_fails_over(two_stubs):
    active, standby = two_stubs
    active.mode = "hang"
    db = FakeDB(active, standby, ACTIVE_KEY, STANDBY_KEY)
    resolver, dispatcher = _stack(db)
    dests = await resolver.resolve(7, "gpt-4")
    object.__setattr__(dests[0], "timeout_seconds", 1)          # bound the hang low for the test
    out = await dispatcher.dispatch(_ctx(), dests, [{"role": "user", "content": "hi"}])
    assert out.text == "from-standby"
    assert out.attempts[0].reason == "timeout"


@pytest.mark.asyncio
async def test_active_401_is_returned_and_standby_untouched(two_stubs):
    active, standby = two_stubs
    active.mode = "unauthorized"
    db = FakeDB(active, standby, ACTIVE_KEY, STANDBY_KEY)
    resolver, dispatcher = _stack(db)
    with pytest.raises(ProviderClientError):
        await dispatcher.dispatch(_ctx(), await resolver.resolve(7, "gpt-4"),
                                  [{"role": "user", "content": "hi"}])
    assert standby.seen_auth == []                              # standby never called (S5)


@pytest.mark.asyncio
async def test_both_down_raises_destinations_exhausted(two_stubs):
    active, standby = two_stubs
    active.mode = "server_error"; standby.mode = "server_error"
    db = FakeDB(active, standby, ACTIVE_KEY, STANDBY_KEY)
    resolver, dispatcher = _stack(db)
    with pytest.raises(DestinationsExhausted) as ei:
        await dispatcher.dispatch(_ctx(), await resolver.resolve(7, "gpt-4"),
                                  [{"role": "user", "content": "hi"}])
    assert ei.value.status_code() == 502


@pytest.mark.asyncio
async def test_breaker_opens_after_three_failures_and_skips_active(two_stubs):
    active, standby = two_stubs
    active.mode = "server_error"
    db = FakeDB(active, standby, ACTIVE_KEY, STANDBY_KEY)
    resolver = DestinationResolver(db, ttl_seconds=0.0)
    registry = DestinationConnectorRegistry(resolver.load_material)
    breaker = DestinationBreaker(failure_threshold=3, cooldown_seconds=300)
    dispatcher = FailoverDispatcher(registry, breaker)
    for _ in range(3):
        await dispatcher.dispatch(_ctx(), await resolver.resolve(7, "gpt-4"),
                                  [{"role": "user", "content": "hi"}])
    active.seen_auth.clear()
    out = await dispatcher.dispatch(_ctx(), await resolver.resolve(7, "gpt-4"),
                                    [{"role": "user", "content": "hi"}])
    assert out.text == "from-standby"
    assert active.seen_auth == []                              # active skipped, breaker open
    assert out.attempts[0].reason == "breaker_open"


@pytest.mark.asyncio
async def test_marker_shape_reports_attempts(two_stubs):
    active, standby = two_stubs
    active.mode = "server_error"
    db = FakeDB(active, standby, ACTIVE_KEY, STANDBY_KEY)
    resolver, dispatcher = _stack(db)
    out = await dispatcher.dispatch(_ctx(), await resolver.resolve(7, "gpt-4"),
                                    [{"role": "user", "content": "hi"}])
    m = out.marker
    assert m["role"] == "standby" and m["provider"] == "openai"
    assert [a["outcome"] for a in m["attempts"]] == ["failed", "ok"]
```

- [ ] **Step 3: Run to verify it fails**

Run: `.venv/bin/pytest tests/unit/failover/ -v --no-cov`
Expected: FAIL first because the module tree is new; once the fixtures + tests are in place, any failure is a real defect in the resolver/registry/dispatcher stack (fix there, not the test).

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/unit/failover/ -v --no-cov`
Expected: PASS (all seven scenarios, driving the real resolver → registry → real `OpenAIConnector` → aiohttp stub path).

- [ ] **Step 5: Commit**

```bash
git add tests/unit/failover/
git commit -m "test(failover): in-process two-stub end-to-end failover scenarios" \
  -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" \
  -m "Claude-Session: https://claude.ai/code/session_013GYHFHJZgh6t5u1v3hTb14"
```

---

## Full gate before merge

After the last task, run the complete gate (spec §8) and fix anything red before opening the PR into the release branch:

```bash
.venv/bin/pytest tests/unit -q          # 90% branch coverage gate (includes tests/unit/failover)
make lint                               # ruff + mypy vs baseline
make openapi-lint                       # spectral on the regenerated spec
make test-security                      # bandit/gitleaks/pip-audit — no new findings
make pre-commit                         # the full pre-commit sequence
```
Every commit message carries the two trailer lines (Global Constraints). The migration (Task 1) runs on SQLite in `test_migration_021.py`; confirm `upgrade`/`downgrade` are reversible there.

## Global Constraints Recap (per-task checklist)

Every task's "done" also means: `from __future__ import annotations` + `@dataclass(slots=True)` on new types; PEP 257 docstrings; `field(repr=False)` on any secret-bearing field; `owner_org_id` (never `org_id`) for tenancy; the failover branch inert (byte-for-byte existing path) when gated off; `.venv/bin/pytest ... -q` green; and (before merge) `make test-unit` still ≥90% branch coverage.

## Self-Review (completed by plan author)

**1. Spec coverage** — every spec section maps to at least one task:

| Spec section | Task(s) |
|---|---|
| §3.1 `provider_credentials.owner_org_id`; material-by-provider-type | T1 (column), T3 (Bedrock JSON parse), T12 (write validation) |
| §3.1 pool-exclusion invariant (mutation-proven) | T7 (`_select_credential`, mutation step) |
| §3.1 platform endpoints exclude tenant rows | T13 |
| §3.1 name-collision `owner_org_id` vs `org_id` | T1 test, T7 test, Global Constraints |
| §3.2 `model_destinations` table + constraints (≤5, unique, checks) | T1 (schema), T12 (≤5 API cap) |
| §3.2 ownership invariant enforced ×3 (write/resolve/build) | T12 (write 422), T8 (resolve SQL predicate), T9 (build assert) |
| §3.2 proxy read-side (deviation: executesql) | T8 (documented deviation) |
| §4 control-plane routes + gate + tenant/IDOR + scopes + DTO/masking | T2 (scopes), T12 (routes/gate/IDOR/masking) |
| §5.1 pipeline placement + `local_only`/`provider_pin` + ctx population | T11 (surfacing), T14 (branch) |
| §5.2 `DestinationResolver` (SQL, pin, local_only, TTL) | T8 |
| §5.3 `FailoverDispatcher` (retryable/next, client-error/raise, breaker-skip, exhausted, auth-surface) | T10, T14 (status mapping) |
| §5.4 attempt semantics, timeouts, streaming, first-byte | T10, T11 (`bytes_flushed`) |
| §5.5 `DestinationConnectorRegistry` + Bedrock/Anthropic fixes | T9 (registry), T3 (connector fixes) |
| §5.6 breaker (own params, fed by dispatcher) | T4 (breaker), T10 (feeds it) |
| §5.7 `usage.waddleai.destination` + Prometheus + stats + log | T5 (metrics), T10 (marker), T14 (merge + stats) |
| §6 security invariants S1–S12 | S1 T12/T14, S2 T8/T9/T12, S3 T7, S4 T8/T9/T12, S5 T10/T16, S6 T10/T11, S7 T10/T12, S8 T8, S9 T11/T8, S10 T6/T14, S11 T14, S12 T13 |
| §7 flag + tier gate (mgmt 404/403, proxy degrade) | T6 (proxy gate), T12 (mgmt gate) |
| §8 unit + in-process HTTP stubs + contract | all unit tasks + T16 (stubs) + T15 (openapi) |
| §9 Phase-1 docs + release notes | T15 |
| §10 decisions (attach to logical model; reuse provider_credentials; ai_providers; implicit failover; auth no-failover; Enterprise-gated; UI deferred) | reflected across T1/T8/T10/T12; UI is explicitly out of scope |

**Out of scope (Phase 2 / non-goals per §9) — no tasks, intentionally:** web UI (`Providers.jsx`/`Routing.jsx` + BYOK form + screenshots); Valkey-shared breaker + resolver invalidation; periodic health probes; user/API-key-level destination scopes; §7.3 model-substitution enforcement and the `_pick_final` allow-list leak; mid-stream (post-first-byte) failover; automatic cross-provider model-ID inference.

**2. Placeholder scan:** no `TBD`/`TODO`/"implement later" in any step. The one narrative-bodied step is T12 Step 3's handler bodies, which name the exact model to copy (`create_access_policy`/`update_access_policy` in `model_access_policies.py`), the exact tables/validations, and provide the full helpers + DTOs the handlers use — not a placeholder. The two deliberate spec deviations (executesql resolver; `DestinationAttempt` rename) are called out inline for the orchestrator.

**3. Type consistency:** `Destination` fields (incl. `organization_id`, `owner_org_id`, `credential_version`, `.role`), `CredentialMaterial`, `DestinationResolver.resolve(...)`/`load_material(...)`, `DestinationConnectorRegistry.get(...)`/`OwnershipError`, `DestinationBreaker.is_open/reserve_probe/record_success/record_failure/snapshot`, `FailoverDispatcher.dispatch(...)`, `Outcome`/`.marker`, `DestinationAttempt`, `DestinationsExhausted.status_code()/retry_after()`, `FailoverGate.evaluate(...)->(bool,str)`, and `is_retryable`/`classify_failure` are used identically in Tasks 8→16 and match the Key Interfaces block. The new attempt type is `DestinationAttempt` everywhere (never `AttemptRecord`, which stays the retry-record type in `llm_connectors.py`). `PipelineContext` gains exactly `local_only`/`provider_pin`/`bytes_flushed`/`destination`, all added once in T11 and consumed in T8-args/T14. `RouteDecision.clamp_local` is added in T11 and read in T11's RoutingStage copy.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-09-04-provider-destination-failover.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task with two-stage review, wave by wave. Wave 1 (T1–T6) has six independent, disjoint-file tasks that can run in parallel; Wave 2 (T7–T13) after Wave 1; Wave 3 (T14–T16) after Wave 2. REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**2. Inline Execution** — execute tasks in this session using superpowers:executing-plans, batch execution with checkpoints for review.

All 16 tasks are pure unit-tested (Task 16's stubs are in-process aiohttp on loopback — no container, no live service). Later tasks consume earlier interfaces exactly as declared in the Key Interfaces block, so execute in wave order.

**Which approach?**
