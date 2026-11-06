# vLLM on Ray GPU Fleet — Plan B, part 2 of 6: Tasks B3–B4 (GPU inventory parsers; fit calculator)

> Part of `2026-11-05-vllm-ray-gpu-fleet-B-fleet-backend.md` (index). Global Constraints, Review Focus, and decisions D1–D9 are in the index and bind every task here. Tasks, Files, Interfaces, and steps below are unchanged from the index plan.

### Task B3: GPU inventory parsers and collector

**Files:**
- Create: `shared/fleet/vllm_ray/__init__.py` (empty docstring-only package marker)
- Create: `shared/fleet/vllm_ray/inventory.py`
- Create: `tests/unit/fleet/vllm_ray/__init__.py`, `tests/unit/fleet/vllm_ray/test_inventory.py`
- Create: `tests/unit/fleet/vllm_ray/fixtures/nvidia_smi_4090_4070.csv`, `rocm_smi_mi300x.json`, `xpu_smi_max1100.json` (fixtures; see Step 1)

**Interfaces:**
- Consumes: `GpuVendor`, `InventoryState` (`shared/fleet/enums.py`, Task B1); `FleetBackend` rows (B2).
- Produces (master, exact):
  - `@dataclass(slots=True) class GpuRecord: node_uid:str; node_name:str; gpu_index:int; vendor:int; gpu_class:str|None; product_name:str; vram_total_mb:int; vram_used_mb:int; pci_bus_id:str; schedulable:bool; state:int`
  - `def parse_nvidia_smi(csv_text: str, class_map: dict[str, str]) -> list[GpuRecord]`
  - `def parse_rocm_smi(json_text: str) -> list[GpuRecord]`
  - `def parse_xpu_smi(json_text: str) -> list[GpuRecord]`
  - `def normalize_class(vendor: int, product_name: str, class_map: dict[str, str]) -> str | None`
  - `class InventoryCollector: def __init__(self, backend_id: int, org_id: int) ; def collect(self, client: RayServeClient) -> list[GpuRecord]` (async; not on Protocol — takes the client as an argument)
  - `async def upsert_inventory(db: Any, fleet_backend_id: int, org_id: int, records: list[GpuRecord]) -> None`

Parser outputs leave `node_uid=""` and `node_name=""`. `InventoryCollector` fills them with `dataclasses.replace` from the per-worker payload. Mixed-node detection happens in the collector, since it needs the whole node.

- [ ] **Step 1: Create fixtures and write failing tests**

Fixtures (realistic output, one per tool; mark the file header comment "fixture: verify against pinned tool version in Plan A image"):

`nvidia_smi_4090_4070.csv` — output of `nvidia-smi --query-gpu=index,name,memory.total,memory.used,pci.bus_id --format=csv,noheader,nounits`:
```
0, NVIDIA GeForce RTX 4090, 24564, 512, 00000000:01:00.0
1, NVIDIA GeForce RTX 4090, 24564, 0, 00000000:02:00.0
2, NVIDIA GeForce RTX 4070, 12282, 300, 00000000:41:00.0
3, NVIDIA RTX A9000, 24564, 0, 00000000:81:00.0
```

`rocm_smi_mi300x.json` — `rocm-smi --showmeminfo vram --showproductname --json`:
```json
{"card0": {"VRAM Total Memory (B)": "206158430208", "VRAM Total Used Memory (B)": "1073741824", "Card Series": "AMD Instinct MI300X", "PCI Bus": "0000:05:00.0"},
 "card1": {"VRAM Total Memory (B)": "206158430208", "VRAM Total Used Memory (B)": "0", "Card Series": "AMD Instinct MI300X", "PCI Bus": "0000:06:00.0"},
 "system": {"Driver version": "6.8.5"}}
```

`xpu_smi_max1100.json` — `xpu-smi discovery -j`:
```json
{"device_list": [
  {"device_id": 0, "device_name": "Intel(R) Data Center GPU Max 1100", "memory_physical_size_byte": 51539607552, "pci_bdf_address": "0000:03:00.0"},
  {"device_id": 1, "device_name": "Intel(R) Data Center GPU Max 1100", "memory_physical_size_byte": 51539607552, "pci_bdf_address": "0000:04:00.0"}
]}
```

Class map used in tests: `{"*RTX 4090*": "nvidia-rtx4090", "*RTX 4070*": "nvidia-rtx4070"}` (fnmatch, case-insensitive).

`test_inventory.py` tests:

```python
# fixtures: FIX_NV (nvidia_smi_4090_4070.csv), FIX_ROCM, FIX_XPU; CLASS_MAP = {"*RTX 4090*": "nvidia-rtx4090", "*RTX 4070*": "nvidia-rtx4070"}
def test_parse_nvidia_smi_classifies_matched_models():
    # classes == ["nvidia-rtx4090", "nvidia-rtx4090", "nvidia-rtx4070", None]; recs[0]: vram_total_mb 24564, vram_used_mb 512, pci_bus_id "00000000:01:00.0", vendor NVIDIA
def test_unmatched_nvidia_is_unclassified_and_unschedulable():  # Review Focus 1
    # recs[3]: state UNCLASSIFIED, schedulable False, gpu_class None, product_name "NVIDIA RTX A9000"
def test_classified_nvidia_is_warm_and_schedulable():
    # recs[0]: state WARM, schedulable True
def test_parse_nvidia_smi_rejects_malformed_row():
    # "0, NVIDIA RTX 4090, 24564\n" -> ValueError (5 fields required)
def test_parse_nvidia_smi_empty_output_returns_empty_list():
    # "" -> [] (the caller treats zero records as a failed reconcile)
def test_degraded_sentinel_all_zero_payload_is_cold():  # AM19d
    # "0, NVIDIA GeForce RTX 4090, 0, 0, 00000000:01:00.0" -> state COLD, schedulable False
def test_parse_rocm_smi_converts_bytes_to_mb_and_normalizes_class():
    # gpu_index [0, 1]; recs[0].vram_total_mb == 196608 (206158430208 // 1048576); vram_used_mb == 1024; gpu_class "amd-mi300x"; vendor AMD
def test_parse_xpu_smi_reports_single_gpu_classes_and_zero_used():
    # 2 records; vram_used_mb == 0 for both (xpu-smi dump needed; v1 gap); vendor INTEL; gpu_class "intel-max1100"
def test_mixed_amd_node_is_mixed_unsupported():
    # _classify_node on "AMD Instinct MI300X" + "AMD Instinct MI250X" -> all state MIXED_UNSUPPORTED, schedulable False
def test_normalize_class_amd_intel_strip_vendor_words():
    # normalize_class(AMD, "AMD Instinct MI300X", {}) == "amd-mi300x"; normalize_class(INTEL, "Intel(R) Data Center GPU Max 1100", {}) == "intel-max1100"
def test_normalize_class_nvidia_unmatched_returns_none():
    # normalize_class(NVIDIA, "NVIDIA RTX A9000", CLASS_MAP) is None
def test_collector_fills_node_fields_from_payload():
    # stub payload node_uid "u1", node_name "n1" -> every record has node_uid "u1", node_name "n1"
def test_empty_raw_uses_allocatable_and_node_class():  # AM19d
    # payload raw "", allocatable_gpus 2, node_class "nvidia-rtx4090" -> 2 records, state COLD, schedulable False, gpu_class "nvidia-rtx4090"
def test_upsert_inventory_inserts_then_updates_same_key():
    # two upserts with the same (backend, node_uid, gpu_index) -> 1 row; vram_used_mb holds the latest value
def test_upsert_marks_absent_gpus_cold():
    # row not in the latest records for this backend -> state COLD, schedulable False
```

The `run_inventory` payload shape (D5): each item `{"node_uid": str, "node_name": str, "vendor": "nvidia"|"amd"|"intel", "class_map": {...}, "raw": str}`, where `raw` is the tool's stdout; optional `allocatable_gpus` (int, the node's allocatable GPU count) and `node_class` (str or None, from the node label) apply only when `raw` is empty. B3 tests inject `_StubInventoryClient`, defined in `test_inventory.py` with one method, `async def run_inventory(self)`, that returns the payload list. `FakeRayServeClient` (created in B6) is not used by B3.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/fleet/vllm_ray/test_inventory.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shared.fleet.vllm_ray.inventory'`

- [ ] **Step 3: Implement `inventory.py`**

- `normalize_class`: NVIDIA → first `class_map` pattern matching `product_name` via `fnmatch.fnmatchcase` on the uppercased name vs uppercased pattern; no match → `None`. AMD/INTEL → `f"{prefix}-{re.sub(r'[^a-z0-9]', '', name.lower() minus vendor words)}"` where vendor words are removed: `amd`, `instinct`, `intel`, `r`, `data`, `center`, `gpu`, `(r)`. Prefix `amd` or `intel`.
- `parse_nvidia_smi`: split lines, `csv` fields stripped; field count must be 5 else `ValueError`. State: class None → `UNCLASSIFIED`, `schedulable=False`; else `WARM`, `schedulable=True`. Vendor `NVIDIA`.
- `parse_rocm_smi`: `json.loads`; keys matching `card\d+` in index order; bytes // 1048576 for total/used; `Card Series` is product name; `PCI Bus` is pci id. Vendor `AMD`. State `WARM` with `schedulable=True` (mixed detection in collector).
- `parse_xpu_smi`: `device_list`; `memory_physical_size_byte // 1048576`; `vram_used_mb=0`; vendor `INTEL`.
- Every parser: `vram_total_mb == 0` (degraded sentinel) → `state=COLD`, `schedulable=False`.
- Empty `raw` with `allocatable_gpus=N`: N records, `state=COLD`, `schedulable=False`, `gpu_class=node_class`, `product_name=""`, `vram_total_mb=0`.
- `InventoryCollector._classify_node(records)`: if vendor is AMD or INTEL and the set of `product_name` has size ≥ 2 → every record `state=MIXED_UNSUPPORTED, schedulable=False`.
- `collect(client)`: `payloads = await client.run_inventory()`; per payload dispatch by `vendor` string (`nvidia`→`parse_nvidia_smi`, `amd`→`parse_rocm_smi`, `intel`→`parse_xpu_smi`; unknown vendor → `ValueError`); `replace()` node fields; `_classify_node`; return flat list. Zero payloads returned → return `[]` and log a WARNING (the backend turns zero-examined into a failed reconcile, not a pass).
- `upsert_inventory`: wrap the sync DB calls in `asyncio.to_thread`; upsert on `(fleet_backend_id, node_uid, gpu_index)`; rows for this backend not in `records` → `state=COLD`, `schedulable=False`. `last_seen = datetime.utcnow()`.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/fleet/vllm_ray/test_inventory.py -v --cov=shared.fleet.vllm_ray.inventory --cov-branch --cov-report=term-missing`
Expected: summary `15 passed, 0 failed`; `inventory.py` coverage ≥ 90%.

- [ ] **Step 5: Commit**

```bash
git add shared/fleet/vllm_ray/__init__.py shared/fleet/vllm_ray/inventory.py tests/unit/fleet/vllm_ray/
git commit -m "feat: add vllm_ray GPU inventory parsers and collector" -m "<the executing session's attribution trailer>"
```

---

### Task B4: Fit calculator and layout recommendation

**Files:**
- Create: `shared/fleet/vllm_ray/fit.py`
- Create: `tests/unit/fleet/vllm_ray/test_fit.py`

**Interfaces:**
- Consumes: `GpuRecord`, `GpuVendor`, `Quantization`, `LayoutStrategy`, `InventoryState` (B1, B3).
- Produces (master, exact): `Pool`, `Layout`, `FitResult` dataclasses; `estimate_weight_bytes(params_b: float, quantization: int, dtype: str) -> int`; `fit_check(params_b: float, quantization: int, dtype: str, max_model_len: int, pools: list[Pool], allow_mixed_class: bool, kv_headroom_pct: int = 20) -> FitResult`; `QUANT_MATRIX: dict[int, set[int]]`.
- Added here (D8, D1-consistent): `quant_allowed(vendor: int, gpu_class: str, quantization: int) -> bool`, `build_pools(records: list[GpuRecord]) -> list[Pool]`, `class_label(gpu_class: str) -> str`, `CONTEXT_MB = 1536`.

Formulas (all integers; use `math.ceil`):

- `bytes_per_param`: bf16/fp16 → 2; fp8 → 1; AWQ/GPTQ → 0.5 × 1.1 overhead; BNB → 0.5 × 1.15 overhead. Quantization NONE uses dtype (`"bfloat16"`/`"float16"` → 2; `"fp8"` dtype string is not used).
- `weight_mb = ceil(params_b * 1e9 * bytes_per_param / 1048576)`
- `total_mb = ceil(weight_mb * (1 + kv_headroom_pct / 100))`
- `per_gpu_mb = ceil(total_mb / (tp * pp)) + CONTEXT_MB` (1536 MB)
- A layout needs `per_gpu_mb ≤ min(free VRAM of each GPU it uses)`; `free = vram_total_mb - vram_used_mb`.
- `max_model_len` is accepted and passed through; it does not change the fit math in v1 (KV headroom is the knob). Docstring says so.

Layout enumeration (for a candidate `(tp, pp)` only, with `tp ∈ divisors` of the node GPU count, tp ≤ gpu count):

- Intel: only `tp=1, pp=1`. Single-GPU replicas.
- Single-node (pp=1): for each pool and each node in it, for each tp in divisors of `gpu_count` where the per-GPU fit holds on all GPUs used: `replicas = gpu_count // tp`, nodes_used=1. Each replica uses tp GPUs on one node.
- Multi-node PP (pp ≥ 2), same class only (default): for each pool, choose pp nodes with `gpu_count ≥ tp` and per-GPU fit; `replicas = 1`; `nodes_used = pp`; `strategy = STRICT_SPREAD`.
- Mixed-class PP (`allow_mixed_class=True`): nodes across all same-vendor pools, each stage homogeneous; same fit rule; `replicas = 1`.
- Ranking key: `(nodes_used, -replicas, per_gpu_mb)`. Layout with the smallest key is `recommended=True`; others `False`. Layouts sorted by key.
- `vram_needed_mb = per_gpu_mb * tp * pp * replicas`; `vram_available_mb = sum(free of GPUs used)`.

Refusals:

- Quantization gate: if no pool of the request's vendor allows the quantization (`quant_allowed` false for every pool), `FitResult(fits=False, layouts=[], refusal=<quant message>)` where the quant message is `f"{QUANT_NAME} is not supported on {pool_label}. Choose {alternatives}."` with `alternatives` = the quantizations that are allowed on that pool (plus bf16 "bf16/fp16"); BNB is never listed as an alternative. Example: `"FP8 is not supported on RTX 3090. Choose AWQ, GPTQ, or bf16/fp16."`
- Empty `pools` (nothing schedulable): refusal = `No schedulable GPU pool. Check that GPUs are classified (see inventory).`
- No fitting layouts: `refusal = "Does not fit in any pool. Options: " + "; ".join(options)`, where options are, in order:
  1. if quantization is NONE and an AWQ layout fits (compute with AWQ), `f"use 4-bit quantization (needs {ceil(total_mb_awq/1024)} GB)"`;
  2. for each pool with a multi-node PP layout that fits (pp = number of nodes in pool up to 3), `f"span {pp} nodes of {class_label(pool.gpu_class)}"`;
  3. if none: `f"add GPUs with at least {ceil(per_gpu_needed/1024)} GB VRAM each"`.

Explanations (exact formats; `need_gb = round(vram_needed_mb / 1024)`, `avail_gb = round(vram_available_mb / 1024)`, Python `round` on floats):

- single node, replicas==1: `"Fits on 1 node using {tp}× {class_label} ({need_gb} GB of {avail_gb} GB). Recommended."` (drop " Recommended." when not recommended, keep the rest identical)
- single node, replicas>1: `"Fits on 1 node using {tp}× {class_label} per replica, {replicas} replicas ({need_gb} GB of {avail_gb} GB)."` + ` Recommended.` when recommended.
- multi-node PP same class: `"Fits across {pp} nodes using {tp}× {class_label} per stage ({need_gb} GB of {avail_gb} GB)."` + ` Recommended.` when recommended.
- multi-node mixed class: `"Fits across {pp} nodes, mixing {labels} stages ({need_gb} GB of {avail_gb} GB)."` where labels = `" and ".join` of distinct class labels in stage order.

`class_label(gpu_class)`: strip vendor prefix (`nvidia-`, `amd-`, `intel-`); `rtx(\d+)` → `RTX {n}`; `mi(\d+)x?` → `MI{n}X`-style uppercase; otherwise uppercase. Examples: `nvidia-rtx4090` → `RTX 4090`; `amd-mi300x` → `MI300X`; `intel-max1100` → `MAX1100`.

QUANT_MATRIX (vendor → allowed quantization set; class qualifiers in `quant_allowed`):
- NVIDIA: `{NONE, AWQ, GPTQ, BNB, FP8}`; FP8 only when `gpu_class` starts with one of `nvidia-rtx40`, `nvidia-rtx50`, `nvidia-l4`, `nvidia-l40`, `nvidia-h100`, `nvidia-h200`, `nvidia-b200` (Ada or newer).
- AMD: `{NONE, AWQ, GPTQ, FP8}`; FP8 only when class starts with `amd-mi300`.
- INTEL: `{NONE}`.

`build_pools(records)`: keep only `schedulable=True` and state `WARM`; group by `(vendor, gpu_class)`; each `Pool.nodes` is a list of `(node_uid, gpu_count, vram_free_mb per gpu)` — per-node: count of GPUs in this class on that node, and the minimum free MB among them.

- [ ] **Step 1: Write the failing table-driven tests** (`test_fit.py`)

Helper `pool(vendor, cls, *nodes)` builds `Pool`; a 4090 GPU has `vram_total_mb=24564`, free per GPU `24564` (empty node).

Expected values (verified by hand with the formulas above):

| Case | Pool | Params, quant, dtype, pp | Expected (asserted literally) |
|---|---|---|---|
| A. 12B, 2× 4090 | one node, 2× RTX 4090 (24564 MB free each) | 12, NONE, bfloat16, pp=1 | `fits=True`; one layout `tp=2, pp=1, replicas=1, nodes_used=1, recommended=True, vram_needed_mb=30540, vram_available_mb=49128`; tp=1 rejected (29003 MB per GPU > 24564). Explanation `"Fits on 1 node using 2× RTX 4090 (30 GB of 48 GB). Recommended."` |
| B. 12B, 1× 4090 | one node, 1× RTX 4090 | 12, NONE, bfloat16 | `fits=False`; refusal `"Does not fit in any pool. Options: use 4-bit quantization (needs 8 GB)."` (AWQ at tp=1 = 9090 MB per GPU fits) |
| C. 70B AWQ, 4× 4090 | one node, 4× RTX 4090 | 70, AWQ, float16, pp=1 | `fits=True`; two layouts, sorted: (1) `tp=2, replicas=2, nodes_used=1, recommended=True, vram_needed_mb=94268, vram_available_mb=98256`, explanation `"Fits on 1 node using 2× RTX 4090 per replica, 2 replicas (92 GB of 96 GB). Recommended."`; (2) `tp=4, replicas=1, nodes_used=1, recommended=False, vram_needed_mb=50208`, explanation `"Fits on 1 node using 4× RTX 4090 (49 GB of 96 GB)."`; tp=1 rejected (45597 MB per GPU) |
| D. 120B BF16, 2 nodes × 4× 4090 | two nodes, 4× RTX 4090 each | 120, NONE, bfloat16, pp=2 | `fits=False`; refusal `"Does not fit in any pool. Options: use 4-bit quantization (needs 74 GB)."` (no `span` option: bf16 pp=2 needs 35869 MB per GPU) |
| E. 120B AWQ, 2 nodes × 2× 4090 | two nodes, 2× RTX 4090 each | 120, AWQ, float16 | `fits=True`; one layout `pp=2, tp=2, replicas=1, nodes_used=2, strategy=STRICT_SPREAD, recommended=True, vram_needed_mb=81676, vram_available_mb=98256`; explanation `"Fits across 2 nodes using 2× RTX 4090 per stage (80 GB of 96 GB). Recommended."`; single-node and tp=1 pp=2 rejected (39302 MB per GPU) |
| F. 120B AWQ, 3 nodes × 4× 4090 | three nodes, 4× RTX 4090 each | 120, AWQ, float16 | `fits=True`; layouts include `pp=3, tp=2, replicas=1, nodes_used=3` (14125 MB per GPU) and `pp=2` layouts; recommended = `tp=4, pp=1, nodes_used=1` (20419 MB per GPU); every `nodes_used=2` layout sorts before the `pp=3` layout |
| G. 120B AWQ, mixed classes off | pools: one node 2× RTX 4090 and one node 2× RTX 4070 (12282 MB each) | 120, AWQ, float16, allow_mixed_class=False | `fits=False`; refusal `"Does not fit in any pool. Options: add GPUs with at least 39 GB VRAM each."` |
| H. 120B AWQ, mixed classes on | pools: one node 4× RTX 4090 and one node 4× RTX 4070 (12282 MB each) | 120, AWQ, float16, allow_mixed_class=True | layouts include `tp=4, pp=2, replicas=1, nodes_used=2, vram_needed_mb=87824, vram_available_mb=147384`, explanation `"Fits across 2 nodes, mixing RTX 4090 and RTX 4070 stages (86 GB of 144 GB)."` (RTX 4090 stage first, pool order); recommended stays `tp=4, pp=1` on the 4090 node (20419 MB per GPU); the 4070 node alone hosts nothing (20419 > 12282) |
| I. 12B AWQ, Intel | one node, 2× intel-max1100 with vram_total_mb=16384 | 12, AWQ, float16 | one layout `tp=1, pp=1, replicas=2, nodes_used=1, recommended=True, vram_needed_mb=18180, vram_available_mb=32768`; no tp=2 layout (Intel rule) |
| J. FP8 on pre-Ada NVIDIA | one node, 2× nvidia-rtx3090 (24564 MB each) | 12, FP8, bfloat16 | `fits=False`; refusal exactly `"FP8 is not supported on RTX 3090. Choose AWQ, GPTQ, or bf16/fp16."` (quantization gate; no fit computed) |
| K. FP8 on Ada | one node, 2× RTX 4090 | 12, FP8, bfloat16 | two layouts, sorted: `tp=1, replicas=2, nodes_used=1, recommended=True, vram_needed_mb=30540`; `tp=2, replicas=1, nodes_used=1, recommended=False, vram_needed_mb=16806` |
| L. Unclassified excluded (Review Focus 1) | one NVIDIA record, product `NVIDIA RTX A9000`, state UNCLASSIFIED | 12, NONE | `build_pools` returns `[]`; `fit_check` returns `fits=False` with refusal `"No schedulable GPU pool. Check that GPUs are classified (see inventory)."` |
| M. Mixed AMD node excluded | two records (MI300X and MI250X), state MIXED_UNSUPPORTED | any | `build_pools` returns `[]`; same refusal as L |

Refusal option 3 uses `per_gpu_needed_mb = ceil(total_mb / G) + CONTEXT_MB`, where `G` is the GPU count of the largest node across the request's pools. Row G: `G=2`, `ceil(75532/2) + 1536 = 39302` MB, shown as 39 GB. The expected values above are final; tests assert them literally.

Also add:
- `test_estimate_weight_bytes_table`: bf16 12B → `24_000_000_000`; AWQ 70B → `int(70e9 * 0.5 * 1.1)`; BNB 10B → `int(10e9*0.5*1.15)`; fp8 → `params*1e9`.
- `test_quant_matrix_matches_spec`: `QUANT_MATRIX[GpuVendor.INTEL] == {Quantization.NONE}`.
- `test_quant_allowed_fp8_by_class` parametrized over `nvidia-rtx4090` (True), `nvidia-rtx3090` (False), `amd-mi300x` (True), `amd-mi250` (False).

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/fleet/vllm_ray/test_fit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'shared.fleet.vllm_ray.fit'`

- [ ] **Step 3: Implement `fit.py`** per the formulas, enumeration, ranking, refusals, and explanation formats above. Each function gets a 2–3 line docstring (project rule).

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/fleet/vllm_ray/test_fit.py -v --cov=shared.fleet.vllm_ray.fit --cov-branch --cov-report=term-missing`
Expected: summary `19 passed, 0 failed` (13 table rows A–M, plus 6 Also-add cases); `fit.py` coverage ≥ 90%.

Run: `mypy --strict shared/fleet/vllm_ray/fit.py`
Expected: `Success: no issues found`.

- [ ] **Step 5: Commit**

```bash
git add shared/fleet/vllm_ray/fit.py tests/unit/fleet/vllm_ray/test_fit.py
git commit -m "feat: add vllm_ray fit calculator and layout recommendation" -m "<the executing session's attribution trailer>"
```
