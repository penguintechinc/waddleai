# vLLM on Ray GPU Fleet — Plan B, part 1 of 6: Tasks B1–B2 (enums, BackendType, NodeInfo; migration 028 and ORM)

> Part of `2026-11-05-vllm-ray-gpu-fleet-B-fleet-backend.md` (index). Global Constraints, Review Focus, and decisions D1–D9 are in the index and bind every task here. Tasks, Files, Interfaces, and steps below are unchanged from the index plan.

### Task B1: Enums, BackendType, NodeInfo fields, registry entry

**Files:**
- Create: `shared/fleet/enums.py`
- Modify: `shared/fleet/base.py:18-27` (BackendType — add `VLLM_RAY = "vllm_ray"` after line 27), `shared/fleet/base.py:58-72` (NodeInfo — append two defaulted fields after `healthy: bool`, line 72)
- Modify: `shared/fleet/registry.py:32-40` (`_MODULE_MAP` — add `BackendType.VLLM_RAY: "shared.fleet.vllm_ray.backend"`)
- Modify: `tests/unit/fleet/test_fleet_base.py:84-95` (enum-set assertion gains `"vllm_ray"`)
- Modify: `tests/unit/fleet/test_fleet_registry.py:244-252` (parametrize gains `(BackendType.VLLM_RAY, "shared.fleet.vllm_ray.backend")`)
- Test: `tests/unit/fleet/test_enums.py`

**Interfaces:**
- Consumes: none.
- Produces: `GpuVendor`, `Quantization`, `DeploymentStatus`, `InventoryState`, `LayoutStrategy`, `PriceMode` (AM6; `AUTO_TCO=1`, `MANUAL=2`), `LocalDeploymentKind` (AM10; `VLLM=1`, `OLLAMA=2`, `LLAMACPP=3`) (members listed in Step 3); `BackendType.VLLM_RAY`; `NodeInfo.gpu_vendor: int | None = None`, `NodeInfo.gpu_class: str | None = None`.

- [ ] **Step 1: Write the failing tests** (`tests/unit/fleet/test_enums.py`)

```python
from shared.fleet.enums import DeploymentStatus, GpuVendor, InventoryState, LayoutStrategy, LocalDeploymentKind, PriceMode, Quantization
from shared.fleet.base import BackendType, NodeInfo

def test_gpu_vendor_values() -> None:
    assert {m.name: int(m) for m in GpuVendor} == {"NVIDIA": 1, "AMD": 2, "INTEL": 3}

def test_quantization_values() -> None:
    assert {m.name: int(m) for m in Quantization} == {"NONE": 0, "AWQ": 1, "GPTQ": 2, "FP8": 3, "BNB": 4}

def test_deployment_status_values() -> None:
    assert {m.name: int(m) for m in DeploymentStatus} == {
        "PENDING": 1, "PULLING": 2, "STARTING": 3, "WARMING": 4,
        "SERVING": 5, "DEGRADED": 6, "STOPPED": 7, "FAILED": 8,
    }

def test_inventory_state_values() -> None:
    assert {m.name: int(m) for m in InventoryState} == {"WARM": 1, "COLD": 2, "UNCLASSIFIED": 3, "MIXED_UNSUPPORTED": 4}

def test_layout_strategy_values() -> None:
    assert {m.name: int(m) for m in LayoutStrategy} == {"STRICT_PACK": 1, "STRICT_SPREAD": 2}

def test_price_mode_values() -> None:
    assert {m.name: int(m) for m in PriceMode} == {"AUTO_TCO": 1, "MANUAL": 2}

def test_local_deployment_kind_values() -> None:
    assert {m.name: int(m) for m in LocalDeploymentKind} == {"VLLM": 1, "OLLAMA": 2, "LLAMACPP": 3}

def test_backend_type_has_vllm_ray() -> None:
    assert BackendType.VLLM_RAY.value == "vllm_ray"

def test_node_info_gpu_fields_are_defaulted_and_positional_compatible() -> None:
    node = NodeInfo("n1", "uid", "k8s", [], 0, 0, True)  # legacy 7-arg call still works
    assert node.gpu_vendor is None and node.gpu_class is None
    node2 = NodeInfo("n1", "uid", "k8s", [], 48000, 40000, True, gpu_vendor=1, gpu_class="nvidia-rtx4090")
    assert (node2.gpu_vendor, node2.gpu_class) == (1, "nvidia-rtx4090")
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/fleet/test_enums.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shared.fleet.enums'`

- [ ] **Step 3: Implement**

`shared/fleet/enums.py`: seven `IntEnum` classes, each with a 2–3 line docstring and exactly these members: `GpuVendor` NVIDIA=1, AMD=2, INTEL=3; `Quantization` NONE=0, AWQ=1, GPTQ=2, FP8=3, BNB=4; `DeploymentStatus` PENDING=1, PULLING=2, STARTING=3, WARMING=4, SERVING=5, DEGRADED=6, STOPPED=7, FAILED=8; `InventoryState` WARM=1, COLD=2, UNCLASSIFIED=3, MIXED_UNSUPPORTED=4; `LayoutStrategy` STRICT_PACK=1, STRICT_SPREAD=2; `PriceMode` AUTO_TCO=1, MANUAL=2 (AM6); `LocalDeploymentKind` VLLM=1, OLLAMA=2, LLAMACPP=3 (AM10). `base.py`: add `VLLM_RAY = "vllm_ray"` to `BackendType` (after line 27), append `gpu_vendor: int | None = None` and `gpu_class: str | None = None` to `NodeInfo` (defaults keep existing positional calls valid). `registry.py`: add the `_MODULE_MAP` entry.


- [ ] **Step 4: Update the two existing assertions**

In `test_fleet_base.py::test_backend_type_enum_values` add `"vllm_ray"` to the set. In `test_fleet_registry.py` add `(BackendType.VLLM_RAY, "shared.fleet.vllm_ray.backend")` to the parametrize list.

- [ ] **Step 5: Run to verify pass**

Run: `pytest tests/unit/fleet/test_enums.py tests/unit/fleet/test_fleet_base.py tests/unit/fleet/test_fleet_registry.py -v`
Expected: all PASS, 0 failures, 0 errors (the module-map test passes without importing `shared.fleet.vllm_ray`, since it inspects `_MODULE_MAP` only).

- [ ] **Step 6: Commit**

```bash
git add shared/fleet/enums.py shared/fleet/base.py shared/fleet/registry.py tests/unit/fleet/test_enums.py tests/unit/fleet/test_fleet_base.py tests/unit/fleet/test_fleet_registry.py
git commit -m "feat: add vllm_ray enums, backend type, and NodeInfo GPU fields" -m "<the executing session's attribution trailer>"
```

---

### Task B2: Migration 028 and ORM models

**Files:**
- Create: `services/management/alembic/versions/028_vllm_ray_fleet.py` (`revision="028_vllm_ray_fleet"`, `down_revision="027_configuration_as_code"`)
- Modify: `services/management/app/models_sqlalchemy.py` — insert `GpuInventory`, `VllmDeployment`, `VllmDeploymentEvent` after `FleetBackend` (class at line 328, ends before `class OllamaDeployment`); add three columns to `TokenUsage` after line 524 (`tokens_saved`); add `public_id`/`params_b` to `ModelRegistry` and `owner_org_id` to `ConnectionLink`
- Test: `tests/unit/management/test_migration_028_vllm_ray.py`

**Interfaces:**
- Consumes: `FleetBackend` (`fleet_backends.id`), `organizations.id`, `TokenUsage` (`token_usage`), `ModelRegistry` (`model_registry`), `ConnectionLink` (`connection_links`).
- Produces: ORM classes `GpuInventory`, `VllmDeployment`, `VllmDeploymentEvent`; `TokenUsage.local_deployment_kind`, `TokenUsage.local_deployment_id`, `TokenUsage.unit_price_input`, `TokenUsage.unit_price_output`; `OllamaDeployment` and `LlamaCppDeployment` pricing columns (AM10); `VllmDeployment.expose_via_proxy`, `.prefer_local`, `.comparison_model`; `ModelRegistry.public_id`, `.params_b`; `ConnectionLink.owner_org_id`; AM6: `VllmDeployment.price_mode`, `.price_breakdown`, `.observed_tokens_per_s`.

Column contract (spec §6.1; match the `public_id` column type already used in `models_sqlalchemy.py` — grep `public_id` and reuse that type):

| Table | Columns |
|---|---|
| `gpu_inventory` | `id` BIGINT PK; `org_id` FK organizations CASCADE NOT NULL; `fleet_backend_id` FK fleet_backends CASCADE NOT NULL; `node_uid` String(255) NOT NULL; `node_name` String(255) NOT NULL; `gpu_index` Integer NOT NULL; `vendor` SmallInteger NOT NULL (GpuVendor); `gpu_class` String(64) NULL; `product_name` String(255) NOT NULL; `vram_total_mb` Integer NOT NULL; `vram_used_mb` Integer NOT NULL default 0; `pci_bus_id` String(64) NOT NULL; `schedulable` Boolean NOT NULL; `state` SmallInteger NOT NULL (InventoryState); `last_seen` DateTime NOT NULL. Unique `(fleet_backend_id, node_uid, gpu_index)` name `uq_gpu_inventory_backend_node_gpu`. |
| `vllm_deployments` | `id` BIGINT PK; `public_id` UUID UNIQUE NOT NULL; `org_id` FK NOT NULL; `fleet_backend_id` FK NOT NULL; `name` String(255) NOT NULL; `slug` String(64) NOT NULL; `model_ref` String(512) NOT NULL; `served_model_name` String(255) NOT NULL; `vendor` SmallInteger NOT NULL; `gpu_class` String(64) NOT NULL; `allow_mixed_class` Boolean NOT NULL default false; `quantization` SmallInteger NOT NULL; `dtype` String(16) NOT NULL; `tensor_parallel` Integer NOT NULL; `pipeline_parallel` Integer NOT NULL; `replicas_min` Integer NOT NULL; `replicas_max` Integer NOT NULL; `max_model_len` Integer NOT NULL; `status` SmallInteger NOT NULL (DeploymentStatus); `status_detail` Text NULL; `serve_app_name` String(128) NOT NULL; `endpoint_url` String(1024) NULL; `connection_link_id` Integer FK nullable (ondelete SET NULL, target table `connection_links`); `expose_via_proxy` Boolean NOT NULL default true; `prefer_local` JSON NULL; `comparison_model` String(255) NOT NULL (commercial "provider/model" for price suggestion + savings, AM1); `price_mode` SmallInteger NOT NULL default 1 (PriceMode, AM6); `price_breakdown` JSON NULL (AM6); `observed_tokens_per_s` Numeric(10,2) NULL (AM6); `price_input_per_1m` Numeric(12,6) NOT NULL; `price_output_per_1m` Numeric(12,6) NOT NULL; `created_at`, `updated_at` DateTime NOT NULL. Unique `(org_id, name)` `uq_vllm_deployments_org_name`; unique `(org_id, slug)` `uq_vllm_deployments_org_slug`. |
| `vllm_deployment_events` | `id` BIGINT PK; `deployment_id` FK vllm_deployments CASCADE NOT NULL, indexed; `ts` DateTime NOT NULL; `state` SmallInteger NOT NULL; `message` Text NOT NULL. |
| `token_usage` (ALTER) | `local_deployment_kind` Integer NULL (LocalDeploymentKind, AM10); `local_deployment_id` BigInteger NULL (no FK; kind-discriminated, replaces the former `fleet_deployment_id` FK); `unit_price_input` Numeric(12,6) NULL; `unit_price_output` Numeric(12,6) NULL. |
| `ollama_deployments`, `llamacpp_deployments` (ALTER, AM10) | `public_id` UUID UNIQUE NOT NULL (backfilled per row like `model_registry`; chargeback and API bodies reference UUIDs, never the integer PK); `price_mode` SmallInteger NOT NULL server_default `1` (PriceMode); `price_input_per_1m`, `price_output_per_1m` Numeric(12,6) NOT NULL server_default `0`; `price_breakdown` JSON NULL; `observed_tokens_per_s` Numeric(10,2) NULL; `comparison_model` String(255) NULL. Same semantics as the `vllm_deployments` columns. |
| `model_registry` (ALTER) | `public_id` UUID UNIQUE NOT NULL, named `uq_model_registry_public_id` — add nullable, backfill one `uuid.uuid4()` per existing row, then alter NOT NULL + unique; `params_b` Numeric(8,2) NULL. |
| `connection_links` (ALTER) | `owner_org_id` BIGINT FK `organizations.id` NULL (NULL = shared as today; set = owned by that org, AM3). |

Slug: `String(64)`, derived from `name` by lowercasing, replacing non `[a-z0-9]` with `-`, collapsing and trimming dashes, truncating to 64. Derivation is implemented in Plan C's API layer; B2 only stores it. `serve_app_name` is `f"vllm-{slug}"` truncated to 128 (set by B7).

- [ ] **Step 1: Write the failing migration test**

Follow `tests/unit/management/test_migration_026_tables.py` (fixture `scratch`, lines 47–66; alembic `Config` pointed at `ALEMBIC_DIR`). Tests:

```python
def test_upgrade_creates_three_tables_and_token_usage_columns(scratch) -> None:
    # gpu_inventory, vllm_deployments, vllm_deployment_events exist after upgrade("028_vllm_ray_fleet")
    # token_usage gains local_deployment_kind, local_deployment_id, unit_price_input, unit_price_output

def test_ollama_and_llamacpp_gain_pricing_columns(scratch) -> None:  # AM10
    # each table has public_id, price_mode, price_input_per_1m, price_output_per_1m, price_breakdown, observed_tokens_per_s, comparison_model

def test_unique_constraints_named_and_enforced(scratch) -> None:
    # duplicate (org_id, name) or (org_id, slug) in vllm_deployments -> IntegrityError; same name in another org_id -> accepted
    # duplicate (fleet_backend_id, node_uid, gpu_index) in gpu_inventory -> IntegrityError

def test_price_columns_not_null_numeric_12_6(scratch) -> None:
    # vllm_deployments.price_input_per_1m and price_output_per_1m: nullable False, Numeric(12, 6)

def test_am1_columns(scratch) -> None:
    # vllm_deployments: expose_via_proxy NOT NULL, comparison_model NOT NULL, prefer_local NULL
    # model_registry: public_id, params_b; connection_links: owner_org_id

def test_am6_pricing_columns(scratch) -> None:
    # vllm_deployments: price_mode NOT NULL (server default 1), price_breakdown NULL, observed_tokens_per_s NULL

def test_model_registry_public_id_backfilled(scratch) -> None:
    # seed 2 model_registry rows (name, role, license, origin, is_utility) at revision 027
    # after upgrade: both public_id values non-null and distinct
    # UPDATE to a duplicate public_id -> IntegrityError; UPDATE to NULL -> IntegrityError

def test_downgrade_removes_tables_and_columns(scratch) -> None:
    # downgrade("027_configuration_as_code"): vllm_deployments absent; added columns absent

def test_orm_matches_migrated_schema(scratch) -> None:
    # GpuInventory, VllmDeployment, VllmDeploymentEvent column names and nullability equal the migrated tables
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/management/test_migration_028_vllm_ray.py -v`
Expected: FAIL — `Can't locate revision 028_vllm_ray_fleet`.

- [ ] **Step 3: Implement the migration and ORM**

Write `028_vllm_ray_fleet.py` with `upgrade()` creating the three tables (in order: `gpu_inventory`, `vllm_deployments`, `vllm_deployment_events`), then `batch_alter_table("token_usage")` adding `local_deployment_kind`, `local_deployment_id`, `unit_price_input`, `unit_price_output` (batch mode is required for SQLite FK add; no FK on the new pair), `batch_alter_table("model_registry")` (add `params_b`; add `public_id` nullable → backfill via bound-parameter UPDATE per row → alter NOT NULL + unique), and `batch_alter_table("connection_links")` (add `owner_org_id` FK). `batch_alter_table` on `ollama_deployments` and `llamacpp_deployments` adds `public_id` (add nullable → backfill → NOT NULL + unique, as for `model_registry`) and the AM10 pricing columns (same server defaults). `downgrade()` drops the altered columns first, then the tables, in reverse order. `vllm_deployments` also carries `price_mode` (`server_default="1"`), `price_breakdown`, `observed_tokens_per_s`. Add the ORM classes with `UniqueConstraint(..., name=...)` matching the migration names exactly. Add `TokenUsage` columns with `Numeric(12, 6)` for prices.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/management/test_migration_028_vllm_ray.py -v`
Expected: 8 PASS.

Run: `pytest tests/unit/management -k "migration" -q`
Expected: existing migration tests PASS (no regressions).

- [ ] **Step 5: Commit**

```bash
git add services/management/alembic/versions/028_vllm_ray_fleet.py services/management/app/models_sqlalchemy.py tests/unit/management/test_migration_028_vllm_ray.py
git commit -m "feat: add vllm_ray schema migration 028 and ORM models" -m "<the executing session's attribution trailer>"
```
