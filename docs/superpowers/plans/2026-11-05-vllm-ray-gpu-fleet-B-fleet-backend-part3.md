# vLLM on Ray GPU Fleet — Plan B, part 3 of 6: Tasks B5–B6 (Ray Serve config; Ray REST client)

> Part of `2026-11-05-vllm-ray-gpu-fleet-B-fleet-backend.md` (index). Global Constraints, Review Focus, and decisions D1–D9 are in the index and bind every task here. Tasks, Files, Interfaces, and steps below are unchanged from the index plan.

### Task B5: Ray Serve config and placement-group spec

**Files:**
- Create: `shared/fleet/vllm_ray/serve_config.py`
- Create: `shared/fleet/vllm_ray/serve_app.py` (`build_authenticated_openai_app`, AM21)
- Create: `tests/unit/fleet/vllm_ray/test_serve_config.py`
- Create: `tests/unit/fleet/vllm_ray/test_serve_app.py`

**Interfaces:**
- Consumes: `VllmDeployment` ORM row (B2) — only attributes `slug`, `serve_app_name`, `served_model_name`, `quantization`, `dtype`, `tensor_parallel`, `pipeline_parallel`, `gpu_class`, `max_model_len`, `replicas_min`, `replicas_max`, `status`; `LayoutStrategy`, `Quantization`, `DeploymentStatus` (B1).
- Produces (master, exact, with D2 extension):
  - `def placement_group_spec(tp: int, pp: int, gpu_class: str | list[str], strategy: int) -> dict[str, Any]`
  - `def build_serve_app(dep: VllmDeployment, image_model_path: str, nccl_env: dict[str, str]) -> dict[str, Any]` — returns ONE application entry (D4: the backend assembles the `{"applications": [...]}` body).

- `serve_app.build_authenticated_openai_app(llm_config: dict, slug: str, keys_dir: str = "/secrets/deployment-keys") -> serve.Application` (AM21). Serve calls it with `args` as keyword arguments. It passes `llm_configs=[llm_config]` to `ray.serve.llm:build_openai_app` and adds the bearer-check middleware (Rules below).

Rules:

- `placement_group_spec`:
  - `strategy == STRICT_PACK`: requires `pp == 1`; `bundles = [{"GPU": 1, f"gpu_class:{c}": 1}] * tp` where `c = gpu_class` (str only). Returns `{"bundles": bundles, "strategy": "STRICT_PACK"}`.
  - `strategy == STRICT_SPREAD`: requires `pp >= 2`. Bundles per stage `i`: `{"GPU": tp, f"gpu_class:{c_i}": tp}` where `c_i = gpu_class[i]` if list (len must equal pp, else `ValueError`) else `gpu_class`. Returns `{"bundles": ..., "strategy": "STRICT_SPREAD"}`.
  - Any other combination → `ValueError("placement strategy/pp mismatch")`.
- `build_serve_app`:
  - `route_prefix = f"/d/{dep.slug}"`; `name = dep.serve_app_name`; `import_path = "ray.serve.llm:build_openai_app"`.
  - `model_source` must start with `/models/`; otherwise `ValueError`.
  - `args.llm_config` (one dict; the wrapper passes `llm_configs=[llm_config]`):
    - `model_loading_config = {"model_id": dep.served_model_name, "model_source": image_model_path}`
    - `engine_kwargs = {"tensor_parallel_size": tp, "pipeline_parallel_size": pp, "quantization": <"awq"|"gptq"|"fp8"|"bitsandbytes"|None>, "dtype": dep.dtype, "max_model_len": dep.max_model_len, "distributed_executor_backend": "ray"}`. Quantization string map: NONE→omit key; AWQ→"awq"; GPTQ→"gptq"; FP8→"fp8"; BNB→"bitsandbytes".
    - `deployment_config = {"autoscaling_config": {"min_replicas": m, "max_replicas": M}}` where `m, M = dep.replicas_min, dep.replicas_max`; when `dep.status == DeploymentStatus.STOPPED`, `m = 0`.
    - `placement_group_config = placement_group_spec(tp, pp, gpu_class, strategy)` where strategy is `STRICT_PACK` if `pp == 1` else `STRICT_SPREAD`.
    - `runtime_env = {"env_vars": dict(nccl_env)}`.
  - Validate `nccl_env` keys are the expected set when provided (`NCCL_SOCKET_NTHREADS`, `NCCL_NSOCKS_PERTHREAD`, `NCCL_SOCKET_IFNAME`, `NCCL_IB_DISABLE`); extra keys are rejected with `ValueError`. NCCL values come from Helm (Plan A), passed by the backend.
- **Auth middleware (AM11, AM16, spec §6.3 layer 2).** `import_path = "shared.fleet.vllm_ray.serve_app:build_authenticated_openai_app"`: wraps `ray.serve.llm:build_openai_app` with a FastAPI middleware. `args["slug"] = dep.slug` (never the token). Per request: read `/secrets/deployment-keys/<slug>` (mtime-cached: re-read only when `st_mtime_ns` changes; kubelet refreshes projected files within ~1 min); compare the `Authorization: Bearer <key>` with `hmac.compare_digest`. Mismatch, or missing/unreadable file, returns 401 (never open). The token never enters the config or `runtime_env`.

- [ ] **Step 1: Write the failing tests** (exact dict assertions)

```python
def test_strict_pack_bundles_per_gpu_with_class_resource():
    # placement_group_spec(2, 1, "nvidia-rtx4090", STRICT_PACK) == {"bundles": [{"GPU": 1, "gpu_class:nvidia-rtx4090": 1}] * 2, "strategy": "STRICT_PACK"}
def test_strict_spread_bundles_per_stage_whole_node():
    # placement_group_spec(2, 3, "nvidia-rtx4090", STRICT_SPREAD)["bundles"] == [{"GPU": 2, "gpu_class:nvidia-rtx4090": 2}] * 3
def test_mixed_class_stages_use_each_stage_class():
    # placement_group_spec(2, 2, ["nvidia-rtx4090", "nvidia-rtx4070"], STRICT_SPREAD)["bundles"] == [{"GPU": 2, "gpu_class:nvidia-rtx4090": 2}, {"GPU": 2, "gpu_class:nvidia-rtx4070": 2}]
def test_mixed_class_list_length_must_equal_pp():  # ["a", "b"] with pp=3 -> ValueError
def test_strict_pack_rejects_pp_gt_1():  # pp=2 with STRICT_PACK -> ValueError
def test_strict_spread_rejects_single_stage():  # pp=1 with STRICT_SPREAD -> ValueError
def test_build_serve_app_exact_shape(dep_factory):
    # dep: slug "devstral-small", serve_app_name "vllm-devstral-small", AWQ, float16, tp=2, pp=1, nvidia-rtx4090, max_model_len 32768, replicas 0..2, STARTING
    app = build_serve_app(dep, "/models/devstral-small-awq", NCCL)
    assert app == {"name": "vllm-devstral-small", "route_prefix": "/d/devstral-small",
        "import_path": "shared.fleet.vllm_ray.serve_app:build_authenticated_openai_app",
        "args": {"slug": "devstral-small", "llm_config": {
            "model_loading_config": {"model_id": "devstral-small", "model_source": "/models/devstral-small-awq"},
            "engine_kwargs": {"tensor_parallel_size": 2, "pipeline_parallel_size": 1, "quantization": "awq", "dtype": "float16", "max_model_len": 32768, "distributed_executor_backend": "ray"},
            "deployment_config": {"autoscaling_config": {"min_replicas": 0, "max_replicas": 2}},
            "placement_group_config": {"bundles": [{"GPU": 1, "gpu_class:nvidia-rtx4090": 1}] * 2, "strategy": "STRICT_PACK"},
            "runtime_env": {"env_vars": NCCL}}}}
def test_middleware_reads_key_from_keys_dir(tmp_path):  # keys_dir=tmp_path; slug file holds token; matching bearer -> 200; missing or wrong bearer -> 401
def test_rotation_picks_up_new_file(tmp_path):  # rewrite file and bump st_mtime_ns; new token 200, old token 401
def test_missing_key_file_returns_401(tmp_path):  # no file for slug -> 401, never open
def test_api_key_value_never_in_config(dep_factory, tmp_path):  # sentinel written to key file; json.dumps(app) lacks it
def test_stopped_deployment_scales_min_to_zero(dep_factory):  # STOPPED with replicas_min 1 -> min_replicas == 0
def test_none_quantization_omits_key(dep_factory):  # Quantization.NONE -> "quantization" not in engine_kwargs
def test_multi_node_uses_strict_spread(dep_factory):  # pp=2 -> placement_group_config strategy "STRICT_SPREAD"
def test_model_source_outside_models_rejected(dep_factory):  # model path "/tmp/x" -> ValueError
def test_nccl_env_rejects_unknown_key():  # extra key "NCCL_FOO" -> ValueError
```

`NCCL = {"NCCL_SOCKET_NTHREADS": "4", "NCCL_NSOCKS_PERTHREAD": "4", "NCCL_SOCKET_IFNAME": "eth0", "NCCL_IB_DISABLE": "1"}`. `dep_factory` is a `types.SimpleNamespace` builder in the test module.

`tests/unit/fleet/vllm_ray/test_serve_app.py`: `test_builder_passes_llm_configs_list_to_build_openai_app(monkeypatch, tmp_path)`. Monkeypatch `ray.serve.llm.build_openai_app` with a recorder; call `build_authenticated_openai_app(llm_config=LLM, slug="s", keys_dir=str(tmp_path))`; assert the recorder was called once with `llm_configs=[LLM]`.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/fleet/vllm_ray/test_serve_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'shared.fleet.vllm_ray.serve_config'`

- [ ] **Step 3: Implement `serve_config.py`** per rules above. Approach: dict literals; no templating.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/fleet/vllm_ray/test_serve_config.py tests/unit/fleet/vllm_ray/test_serve_app.py -v --cov=shared.fleet.vllm_ray.serve_config --cov=shared.fleet.vllm_ray.serve_app --cov-branch --cov-report=term-missing`
Expected: summary `17 passed, 0 failed` (16 in test_serve_config.py, 1 in test_serve_app.py); coverage ≥ 90%.

- [ ] **Step 5: Commit**

```bash
git add shared/fleet/vllm_ray/serve_config.py shared/fleet/vllm_ray/serve_app.py tests/unit/fleet/vllm_ray/test_serve_config.py tests/unit/fleet/vllm_ray/test_serve_app.py
git commit -m "feat: add vllm_ray Serve application and placement-group config" -m "<the executing session's attribution trailer>"
```

---

### Task B6: Ray REST client

**Files:**
- Create: `shared/fleet/vllm_ray/ray_client.py`
- Create: `tests/unit/fleet/vllm_ray/test_ray_client.py`
- Create: `tests/conformance/_fake_ray.py` (`FakeRayServeClient`, consumed by B7 and B9)

**Interfaces:**
- Consumes: none (pure HTTP).
- Produces (master, with D3/D4/D5 changes):
  - `class RayServeClient(Protocol)`:
    - `async def put_applications(self, config: dict[str, Any]) -> None` — `config = {"applications": [...]}`, full desired set (D4)
    - `async def get_application(self, name: str) -> dict[str, Any]` — `{"status": str, "message": str}`; `status="NOT_FOUND"` when absent
    - `async def delete_application(self, name: str, remaining: dict[str, Any]) -> None` — `remaining` is the full config without `name` (D3); implementation PUTs `remaining`
    - `async def run_inventory(self) -> list[dict[str, Any]]` — per-worker payloads (D5)
  - `class RayClientError(RuntimeError)` — carries HTTP status and a truncated (≤200 char) body; never the token.
  - `class HttpRayServeClient(RayServeClient): def __init__(self, head_url: str, token: str | None, timeout_s: float = 10.0)`

- `tests/conformance/_fake_ray.py`: `FakeRayServeClient` implements the four Protocol methods in memory. Attributes: `apps: dict[str, dict]` (app name → config, replaced by each PUT), `puts: list[dict]` (every PUT body), `statuses: dict[str, dict]` (scripted Serve status per app name; missing → `NOT_FOUND`), `inventory_payloads: list[dict]` (returned by `run_inventory`), `fail_next_put` and `fail_next_get` (`Exception | None`, raised once then cleared). `delete_application(name, remaining)` PUTs `remaining`.

Behavior:
- Base URL = `head_url.rstrip("/")`. Non-`https`/`http` schemes → `ValueError`.
- Auth: when `token`, header `Authorization: Bearer <token>`. `__repr__` returns `"HttpRayServeClient(head_url=..., token=***)"`. Every request (PUT, GET, POST, job logs) carries the header. Token precedence (AM15, AM12): env `WADDLEAI_RAY_AUTH_TOKEN`, else the file at `RAY_AUTH_TOKEN_PATH` (default `/etc/ray-auth/token`, the Plan A Secret mount), read once by `HttpRayServeClient.from_env(head_url)`. Neither present, or file empty: `RayClientError("ray auth token unavailable")` at construction. Fail closed; no anonymous mode.
- `put_applications`: `PUT /api/serve/applications/` JSON body; 2xx → return; else `RayClientError`.
- `get_application`: `GET /api/serve/applications/`; response JSON `{"applications": {<name>: {"status": ..., "message": ...}}}`; returns the entry or `{"status": "NOT_FOUND", "message": ""}`.
- `delete_application`: `await self.put_applications(remaining)`.
- `run_inventory`: `POST /api/jobs/` body `{"entrypoint": "python3 -m shared.fleet.vllm_ray.inventory_job", "runtime_env": {}}` → `submission_id`. Poll `GET /api/jobs/{id}` every `poll_interval_s` (constructor kwarg, default 2.0) until `status` in `SUCCEEDED|FAILED|STOPPED`; overall deadline `timeout_s * 6`; then `GET /api/jobs/{id}/logs` → `logs` string. Parse lines beginning with `WAGPU_JSON:` → `json.loads(rest)`. FAILED/STOPPED or deadline → `RayClientError`. Zero parsed payloads on SUCCEEDED → return `[]` (caller decides).
- Timeouts: `httpx.Timeout(timeout_s)`; `httpx.AsyncClient` created per call via `async with` unless `transport` injected (tests inject `httpx.MockTransport`). Constructor kwarg `transport: httpx.AsyncBaseTransport | None = None`.

- [ ] **Step 1: Write the failing tests** (`httpx.MockTransport`, no network)

```python
async def test_put_applications_sends_full_body_and_bearer():
    seen = {}
    def handler(req): seen["auth"] = req.headers["authorization"]; seen["body"] = json.loads(req.content); seen["m"] = req.method; return httpx.Response(200, json={})
    client = HttpRayServeClient("http://head:8265", "tok", transport=httpx.MockTransport(handler))
    await client.put_applications({"applications": [{"name": "a"}]})
    assert seen == {"auth": "Bearer tok", "body": {"applications": [{"name": "a"}]}, "m": "PUT"}

async def test_put_non_2xx_raises_without_token_in_message():
    client = HttpRayServeClient("http://head:8265", "tok", transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
    with pytest.raises(RayClientError) as exc:
        await client.put_applications({"applications": []})
    assert "500" in str(exc.value) and "boom" in str(exc.value) and "tok" not in str(exc.value)

async def test_get_application_returns_status_and_not_found():
    # body {"applications": {"vllm-a": {"status": "DEPLOY_FAILED", "message": "OOM"}}}
    # get_application("vllm-a") == {"status": "DEPLOY_FAILED", "message": "OOM"}; get_application("x")["status"] == "NOT_FOUND"

async def test_delete_application_puts_remaining_config():
    # method PUT, body == remaining

async def test_run_inventory_polls_until_succeeded_and_parses_payloads():
    # POST returns submission_id "j1"; GET /api/jobs/j1 -> RUNNING then SUCCEEDED; logs contain one WAGPU_JSON line and one noise line
    # result == [payload dict]

async def test_run_inventory_failed_job_raises():  # status FAILED -> RayClientError

async def test_run_inventory_deadline_raises(): # status stays RUNNING, poll_interval_s=0, timeout_s=0.01 -> RayClientError

def test_repr_masks_token():  # "tok" not in repr(client); "***" in repr

def test_rejects_non_http_scheme():  # ValueError on "ftp://head"

async def test_timeout_is_applied(): # httpx.TimeoutException from handler surfaces as RayClientError
async def test_every_request_sends_bearer_from_token_path(tmp_path, monkeypatch)  # PUT, GET, POST, job logs each carry Authorization: Bearer <file token>
def test_env_token_beats_token_path(monkeypatch)  # WADDLEAI_RAY_AUTH_TOKEN wins over the file
def test_missing_token_fails_closed(monkeypatch)  # no env, no file -> RayClientError; no request sent
async def test_token_never_logged(caplog)  # full put/get/run_inventory at DEBUG with token SECRET-RAY-TOKEN: absent from caplog.text, error messages and repr
def test_fake_ray_client_roundtrip():  # put_applications stores apps and puts; get_application returns statuses[name]; fail_next_put raises once
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/fleet/vllm_ray/test_ray_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'shared.fleet.vllm_ray.ray_client'`

- [ ] **Step 3: Implement `ray_client.py`** per behavior above. Log request method and path at DEBUG only (never headers or body containing tokens).

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/fleet/vllm_ray/test_ray_client.py -v --cov=shared.fleet.vllm_ray.ray_client --cov-branch --cov-report=term-missing`
Expected: summary `15 passed, 0 failed`; coverage ≥ 90%.

- [ ] **Step 5: Commit**

```bash
git add shared/fleet/vllm_ray/ray_client.py tests/unit/fleet/vllm_ray/test_ray_client.py tests/conformance/_fake_ray.py
git commit -m "feat: add vllm_ray Ray Serve REST client" -m "<the executing session's attribution trailer>"
```
