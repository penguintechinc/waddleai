# vLLM on Ray GPU Fleet — Plan B, part 5 of 6: Tasks B8–B10 (placement/caps, conformance, verification gate; open items)

> Part of `2026-11-05-vllm-ray-gpu-fleet-B-fleet-backend.md` (index). Global Constraints, Review Focus, and decisions D1–D9 are in the index and bind every task here. Tasks, Files, Interfaces, and steps below are unchanged from the index plan.

### Task B8: Placement and caps changes

**Files:**
- Modify: `shared/fleet/caps.py:79-84` (`select_capable_node` — add keyword-only `gpu_class: str | None = None, gpu_vendor: int | None = None`); `caps.py:204-232` (`_placed_model_names_sync` — include vLLM deployments); add `async def vllm_enrolled_node_count(self) -> int` to `CapEnforcer` (after `min_vram_for`, line ~193)
- Modify: `shared/fleet/placement.py:322-` (`place_model`), the `min_vram_gb = ...` block at line 376
- Modify: `services/management/app/services/ollama_manager.py:165` (`OllamaDeploymentManager.create_deployment`; node labels from `config.node_selector` (`OllamaDeploymentConfig`, default `{"gpu": "true"}` at line 76); GPU count from `config.gpu_count`; call the refusal helper before creating)
- Modify: `services/management/app/services/llamacpp_manager.py:362` (`LlamaCppManager.create_deployment`; node labels from `config.node_selector` (`LlamaCppDeploymentConfig`, line 87); GPU count from `config.gpu_count` (line 84). No GPU or node_selector validation exists there today.)
- Create: `shared/fleet/vllm_ray/coexistence.py` (`refuse_plain_gpu_request_on_renamed_node`)
- Create: `tests/unit/fleet/vllm_ray/test_coexistence.py`; add tests to `tests/unit/fleet/test_caps.py` and `tests/unit/fleet/test_placement.py`

**Interfaces:**
- Consumes: `NodeInfo.gpu_vendor`, `NodeInfo.gpu_class` (B1); `GpuInventory`, `VllmDeployment` (B2); `DeploymentStatus` (B1).
- Produces:
  - `select_capable_node(nodes, min_vram_gb, *, gpu_class=None, gpu_vendor=None) -> NodeInfo | None` (defaults keep existing callers unchanged).
  - `CapEnforcer.vllm_enrolled_node_count() -> int`: distinct `node_uid` over `gpu_inventory` rows with `state == WARM` for this org's vllm backends.
  - `refuse_plain_gpu_request_on_renamed_node(node_labels: dict[str, str], resource_requests: dict[str, str]) -> str | None`.
  - `PlacementEngine.place_model`: reads `constraints.get("gpu_class")` and `constraints.get("gpu_vendor")`; runs the capacity selection when `min_vram_gb is not None or gpu_class or gpu_vendor` and `"node_id" not in constraints`.

Rules:
- `_placed_model_names_sync` adds `served_model_name` of `vllm_deployments` rows (org's vLLM backends) with status in (`STARTING`, `WARMING`, `SERVING`, `DEGRADED`) (counts each row as one managed model, spec §3 Licence row).
- `refuse_plain_gpu_request_on_renamed_node`: returns `None` unless `node_labels.get("waddleai.penguintech.io/ai-node") == "true"` AND `"nvidia.com/gpu" in resource_requests` (value > 0). Otherwise returns the message: `"This is an AI node: GPUs are scheduled per model. Request the model's GPU class (nvidia.com/<class>, e.g. nvidia.com/rtx4090) instead of nvidia.com/gpu, or pick a non-AI node."`

- [ ] **Step 1: Write the failing tests**

`test_coexistence.py`:
```python
def test_plain_gpu_request_on_ai_node_refused():
    msg = refuse_plain_gpu_request_on_renamed_node({"waddleai.penguintech.io/ai-node": "true"}, {"nvidia.com/gpu": "1"})
    assert msg is not None and "nvidia.com/rtx4090" in msg and "instead of nvidia.com/gpu" in msg

def test_class_resource_on_ai_node_allowed():
    assert refuse_plain_gpu_request_on_renamed_node({"waddleai.penguintech.io/ai-node": "true"}, {"nvidia.com/rtx4090": "1"}) is None

def test_plain_gpu_on_non_ai_node_allowed(): assert ... is None
def test_zero_plain_gpu_allowed(): {"nvidia.com/gpu": "0"} -> None
```

`test_caps.py` additions:
```python
def test_select_capable_node_filters_by_class_and_vendor():
    # nodes: NodeInfo("a", "uid-a", "k8s", [], 48000, 40000, True, gpu_vendor=1, gpu_class="nvidia-rtx4090") and the same with "b", gpu_class="nvidia-rtx4070"
    # select_capable_node(nodes, 0, gpu_class="nvidia-rtx4070").node_id == "b"
def test_select_capable_node_defaults_unchanged():
    # the existing select_capable_node cases in test_caps.py pass unmodified (no filter arguments)
async def test_vllm_enrolled_node_count_counts_warm_distinct_nodes_only(fake_db):
    # gpu_inventory rows: u1 gpu0 WARM, u1 gpu1 WARM, u2 WARM, u3 COLD -> vllm_enrolled_node_count() == 2
async def test_vllm_deployments_count_toward_free_model_cap(fake_db):
    # org model cap = 3 (set as the existing model-cap tests set it); 3 SERVING rows (a, b, c) + 1 STOPPED row (d)
    # enforce_model_cap("new-model") raises CapExceededError; enforce_model_cap("a") passes; d is not counted
```

`test_placement.py` additions:
```python
async def test_place_model_passes_gpu_class_filter_to_backend(...):
    # list_nodes returns "n1/nvidia-rtx4090" and "n1/nvidia-rtx4070"; place_model("m", {"gpu_class": "nvidia-rtx4070"}).node_id == "n1/nvidia-rtx4070"
async def test_place_model_gpu_class_without_capable_node_raises_insufficient(...):
    # place_model("m", {"gpu_class": "nvidia-l4"}) raises InsufficientCapacityError
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/fleet/vllm_ray/test_coexistence.py tests/unit/fleet/test_caps.py tests/unit/fleet/test_placement.py -v`
Expected: the new tests FAIL (`ImportError` for `coexistence`, `TypeError` for unexpected keyword `gpu_class`, `AttributeError` for `vllm_enrolled_node_count`); existing tests PASS.

- [ ] **Step 3: Implement** per the Rules above. In `ollama_manager.create_deployment` (line 165), compute `node_labels` from `config.node_selector` (or `{"gpu": "true"}` default from line 76) and `resource_requests` from `{"nvidia.com/gpu": str(config.gpu_count)}` when `config.gpu_count > 0`; `if msg := refuse_...(...): raise ValueError(msg)`. Same call in `LlamaCppManager.create_deployment` (llamacpp_manager.py:362). Gate the call behind `is_feature_enabled("waddleai.fleet-vllm-ray", distinct_id=str(org_id), default=False)` so existing behavior is unchanged while the flag is off.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/fleet/vllm_ray/test_coexistence.py tests/unit/fleet/test_caps.py tests/unit/fleet/test_placement.py tests/unit/management -k "ollama or llamacpp or coexist" -v`
Expected: the 10 new tests pass (4 coexistence, 4 caps, 2 placement); the `-k` run and `pytest tests/unit/fleet -q` each report `0 failed`.

Run: `pytest tests/unit/fleet -q`
Expected: 0 failures.

- [ ] **Step 5: Commit**

```bash
git add shared/fleet/caps.py shared/fleet/placement.py shared/fleet/vllm_ray/coexistence.py services/management/app/services/ollama_manager.py services/management/app/services/llamacpp_manager.py tests/unit/fleet/vllm_ray/test_coexistence.py tests/unit/fleet/test_caps.py tests/unit/fleet/test_placement.py
git commit -m "feat: class-aware placement, vLLM caps counting, and AI-node co-tenancy refusal" -m "<the executing session's attribution trailer>"
```

---

### Task B9: Conformance and registry build path

**Files:**
- Modify: `tests/conformance/test_fleet_conformance.py:33-108` (add `vllm_ray_backend` fixture next to the existing ones) and `:113-121` (`BACKENDS` list gains `("vllm_ray_backend", BackendType.VLLM_RAY)`)
- Modify: `tests/conformance/_fake_dal.py` — only if `FakeDAL` lacks a method used by B7 (`insert`, `update`, `select` with `belongs` already exist at lines 85–115); extend, do not replace.
- Create: `tests/conformance/test_vllm_ray_behavior.py`
- Modify: `shared/fleet/registry.py` (`build_backend`: for `BackendType.VLLM_RAY`, pass `{"ray_token": <decrypted>}` as `credentials`; other types unchanged)
- Modify: `tests/unit/fleet/test_fleet_registry.py` — add `test_build_backend_vllm_ray_decrypts_credentials_without_logging_secret`

**Interfaces:**
- Consumes: `VllmRayBackend` (B7), `FakeRayServeClient` (`tests/conformance/_fake_ray.py`, B6), `build_backend` (`shared/fleet/registry.py:88`).
- Produces: conformance coverage for the six ABC methods and the registry path.

- [ ] **Step 1: Write the failing tests**

Conformance (`test_fleet_conformance.py`, through the existing parametrized `BACKENDS` test): the fixture is

```python
@pytest.fixture
def vllm_ray_backend() -> VllmRayBackend:
    return VllmRayBackend(FakeDAL(), config={"head_url": "http://head:8265", "serve_base_url": "http://head:8000", "org_id": 1}, credentials=None, client=FakeRayServeClient())
```

`test_vllm_ray_behavior.py`:
```python
async def test_six_methods_on_empty_fleet(vllm_ray_backend):
    assert await vllm_ray_backend.list_nodes() == []
    assert await vllm_ray_backend.endpoints_for("any") == []
    h = await vllm_ray_backend.health(); assert h.backend_id == vllm_ray_backend.fleet_backend_id and h.node_count == 0
    with pytest.raises(PermissionError): await vllm_ray_backend.deprovision("n")
    with pytest.raises(RuntimeError, match="Deploy it from the GPU Fleet page"): await vllm_ray_backend.place_model("m", {"lazy": True})

async def test_provision_deploys_listed_deployments(...)  # constraints deployment_public_ids -> deploy called; returns list_nodes()

async def test_registry_build_backend_vllm_ray(...)   # build_backend(db, row) with row.type "vllm_ray", row.config, row.credentials_ref
```

Registry test (`test_fleet_registry.py`):
```python
def test_build_backend_vllm_ray_decrypts_credentials_without_logging_secret(monkeypatch, caplog):
    monkeypatch.setattr(registry, "decrypt_credential", lambda ref: "SECRET-RAY-TOKEN-9999")
    row = SimpleNamespace(id=7, type="vllm_ray", management_scope="full_lifecycle", config={"head_url": "http://h:8265", "serve_base_url": "http://h:8000"}, credentials_ref="enc:xyz")
    caplog.set_level(logging.DEBUG)
    backend = registry.build_backend(FakeDAL(), row)
    assert isinstance(backend, VllmRayBackend) and backend.fleet_backend_id == 7
    assert backend.credentials == {"ray_token": "SECRET-RAY-TOKEN-9999"}
    assert isinstance(backend.client, HttpRayServeClient)
    assert "SECRET-RAY-TOKEN" not in caplog.text
```

Decision (binding): `credentials` is `dict[str, str] | None` with key `ray_token` for this backend. `build_backend` (`shared/fleet/registry.py`) passes `{"ray_token": <decrypted value>}` for `BackendType.VLLM_RAY`; other types are unchanged. When `client is None`, `VllmRayBackend.__init__` builds `HttpRayServeClient(config["head_url"], token=credentials["ray_token"])` if `credentials` has `ray_token`, else `HttpRayServeClient.from_env(config["head_url"])` (B6; fail-closed). The test asserts `backend.credentials == {"ray_token": "SECRET-RAY-TOKEN-9999"}` and that the secret is absent from `caplog.text`.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/conformance/test_fleet_conformance.py tests/conformance/test_vllm_ray_behavior.py tests/unit/fleet/test_fleet_registry.py -v -k "vllm or VLLM"`
Expected: FAIL — `ImportError` / `KeyError` for the new fixture and registry path until implemented.

- [ ] **Step 3: Implement** the missing fixture wiring, the lazy `HttpRayServeClient` construction in `VllmRayBackend.__init__` (per the Decision paragraph in Step 1), and any `FakeDAL` method additions required by B7.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/conformance tests/unit/fleet -q`
Expected: summary `0 failed, 0 errors`.

- [ ] **Step 5: Commit**

```bash
git add tests/conformance/test_fleet_conformance.py tests/conformance/test_vllm_ray_behavior.py tests/conformance/_fake_dal.py tests/unit/fleet/test_fleet_registry.py shared/fleet/vllm_ray/backend.py shared/fleet/registry.py
git commit -m "test: add vllm_ray conformance and registry build-path coverage" -m "<the executing session's attribution trailer>"
```

---

### Task B10: Full verification gate

**Files:** none modified (fix-forward only if a gate fails; a fix becomes its own commit with a regression test).

**Interfaces:**
- Consumes: all of B1–B9.
- Produces: evidence block for the PR description (counts).

- [ ] **Step 1: Unit suite through the repo target**

Run: `make test-unit 2>&1 | tee "<the executor session's scratchpad directory (literal path at execution time)>/b10-unit.log"; echo "exit=${PIPESTATUS[0]}"`
Expected: `exit=0`; coverage gate (`fail_under = 90`, `.coveragerc`) passes; log shows the number of collected tests and it is above the CI minimum.

- [ ] **Step 2: Lint, mypy strict on the new package**

Run: `make lint; echo "exit=$?"` — expected `exit=0`.
Run: `mypy --strict shared/fleet/vllm_ray; echo "exit=$?"` — expected `Success: no issues found` and `exit=0`.

- [ ] **Step 3: Coverage on the new package**

Run: `pytest tests/unit/fleet/vllm_ray tests/conformance tests/unit/fleet -q --cov=shared.fleet.vllm_ray --cov=shared.fleet.pricing --cov-branch --cov-report=term-missing --cov-fail-under=90`
Expected: the `shared/fleet/vllm_ray/*` rows and the `shared/fleet/pricing.py` row each ≥ 90% (`--cov-fail-under=90` checks the combined total); exit 0.

- [ ] **Step 4: Gate can fail (verification integrity)**

Run: `sed -i 's/^CONTEXT_MB = 1536/CONTEXT_MB = 0/' shared/fleet/vllm_ray/fit.py; pytest tests/unit/fleet/vllm_ray/test_fit.py -q; echo "exit=$?"`
Expected: `exit=1` (non-zero) with a `N failed` summary. Then `git checkout -- shared/fleet/vllm_ray/fit.py` and `grep -c "^CONTEXT_MB = 1536" shared/fleet/vllm_ray/fit.py` prints `1`. Record the failing count in the PR.

- [ ] **Step 5: Record counts and commit nothing new**

Report to the PR description: number of unit tests collected for `tests/unit/fleet/vllm_ray`, passed/failed counts, coverage percentage for `shared/fleet/vllm_ray`, mypy result, and the Step 4 failure count. Zero examined = FAIL.

---

## Open items the plan could not determine

- Pinned Ray, vLLM, and KubeRay versions (Plan A). The placement-group-to-worker bundle mapping and the exact `ray.serve.llm` config keys (`model_loading_config`, `placement_group_config`, `engine_kwargs`) must be verified against those versions. If the pinned version cannot honor `STRICT_SPREAD` per-stage class bundles, verify on pinned versions; if mapping fails, STOP and report (no fallback executor is planned here). Verification is Plan E4.
- Exact JSON shapes of the Ray Serve REST status response and the Jobs API response. Fixtures and tests in B6 follow the documented shapes; confirm against the pinned Ray version.
- `rocm-smi` and `xpu-smi` JSON key names. Fixtures in B3 are realistic but unverified; confirm on the AMD and Intel images. `xpu-smi` used-VRAM is reported as 0 in v1.
- Fit-calculator constants (`CONTEXT_MB = 1536`, overhead factors) are spec defaults; validate against real loads (spec §9, Plan E).
- `public_id` column type: B2 reuses whatever the existing models use.
- Plan C owns `connection_links` rows, offers, the API layer, the missing-usage counter (Review Focus 3), and the uniqueness/offers tests (Review Focus 5).
