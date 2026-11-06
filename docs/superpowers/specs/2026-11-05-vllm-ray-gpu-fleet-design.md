# vLLM on Ray: Heterogeneous GPU Fleet — Design

**Status:** DRAFT v2 (builder-check resolved), awaiting owner approval
**Branch:** release/v0.4.X
**Date:** 2026-11-05

## 1. Intent

WaddleAI gains a first-class way to deploy and operate open-weight models
(12B–70B routinely, ~120B for special cases) on the company's own
Kubernetes GPU nodes, using vLLM as the inference engine and Ray as the
scheduler across nodes whose GPUs differ by vendor (NVIDIA, AMD, Intel) and
by model (e.g. an RTX 4090 and an RTX 4070 in the same chassis).

**Why:** the company is moving a growing share of inference to self-hosted
open models (Devstral is the first named target) to cut Anthropic/OpenAI
token spend. Local models **augment** commercial providers, they do not
replace them: routing prefers local capacity where it fits and falls back
to commercial when it does not. The people deploying models are technical
but not AI specialists, so the primary surface is a guided admin UI, not
raw API/Helm.

## 2. Goals, non-goals, anti-goals

**Goals**
- Admin can deploy a model from the WaddleAI UI, see whether it fits the
  available GPUs *before* deploying, pick a sensible layout with one click,
  watch it come up, and have it immediately routable through the proxy.
- Single-node replicas (tensor parallel across one node's matching GPUs)
  and multi-node replicas (pipeline parallel across nodes) both supported.
- Heterogeneous GPUs handled correctly: a replica never spans GPU vendors.
  A tensor-parallel group is always one GPU class. Pipeline-parallel
  stages may be different classes (same vendor) only when the admin sets
  `allow_mixed_class`; each stage is still homogeneous. (Call: mine.)
- Real GPU inventory (vendor, class, VRAM total/free) feeds placement.
  Today every fleet provider reports 0 MB VRAM; this feature fixes that
  for the new provider and defines the contract others should follow.
- Weights pulled once per cluster, digest-verified, served to every
  replica; optional per-node NVMe hot cache.
- Savings visible: tokens served locally vs. commercial, in the UI.
- **Chargeback holds for self-hosted models:** every token served by a
  vLLM deployment is metered against the requesting team/org's existing
  virtual token budget (elastic pooled quota) at a per-deployment virtual
  price. Self-hosted never means free or unmetered.
- Everything behind a PostHog flag, default OFF; all tiers get the feature;
  existing node caps gate scale.

**Non-goals (v1)**
- Training, fine-tuning, LoRA hot-swap.
- Multi-cluster / cross-cluster Ray.
- Replacing the existing Ollama / llama.cpp fleet providers.
- Rewriting this in Rust now. The Rust data-plane migration (P5 fleet
  controller) inherits the DB contract defined here; v1 ships in the
  existing Python fleet subsystem because of urgency.
- Intel multi-GPU tensor parallelism (vLLM XPU build does not support it
  reliably as of the pinned version); Intel = single-GPU replicas, stated
  in the UI.

**Anti-goals (failure even if it "works")**
- A deployment that silently falls back to a smaller layout, CPU
  offload, or a different quantization than the admin chose. Misfit must
  be refused with a plain-language reason.
- Placement that mixes a 4090 and a 4070 into one tensor-parallel group
  (throughput collapses to the slowest card, and VRAM mismatch can OOM).
- Ray dashboard / Serve control API reachable from outside the namespace.
- Any model from the existing origin deny-list (`_DENIED_ORIGINS`,
  `shared/fleet/placement.py`; PRC-origin) deployed
  via this path. The placement engine's deny-list already applies.
- Local traffic that bypasses quota or chargeback, or is metered from an
  estimate instead of vLLM's actual `usage` counts (streaming included).
- A fleet UI that assumes AI literacy (no raw "TP/PP/dtype" without an
  explanation and a recommended default).

## 3. Constraints

| Area | Constraint |
|---|---|
| Cluster | Mixed-use K8s (Cilium, Tetragon, PSA `restricted`). Three node builds: **AI** (Threadripper, multiple GPUs incl. mixed models, Samsung 990 Pro NVMe, 2×25G LACP), **General** (7950X-class, 980 NVMe, 2×10G LACP), **Storage** (low CPU, DDR4, HDD + SATA SSD mix, 2×10G LACP). |
| GPUs | NVIDIA, AMD, Intel may coexist in one cluster. Mixed NVIDIA models within one node are common. Consumer cards (24 GB class) are the norm. |
| Network | No RDMA. LACP is per-flow, so a single TCP stream caps at one link (25 Gbps on AI nodes). |
| Models | 12B–70B single-node; ~120B needs pipeline parallelism across 2–3 nodes and 4-bit quantization (bf16 120B ≈ 240 GB weights). |
| Language | Control plane: Python 3.13 + Quart (`services/management`). UI: React 18 + Vite + TS. |
| Deps | Immutably pinned (image digests, exact versions). No PRC-origin deps. vLLM (PyTorch Foundation / UC Berkeley), Ray (Anyscale), KubeRay (CNCF-adjacent, Ray project) are all acceptable. |
| Containers | Rootless, PSA `restricted` by default. `hostPath` needs a documented exception (see §6.4). |
| Flags/License | PostHog flag `waddleai.fleet-vllm-ray` (new, default OFF) **and** existing `waddleai.fleet_v2`. Free tier allowed; existing `CapEnforcer` (5 nodes / 3 models on Free) applies: each `vllm_deployments` row counts as one managed model; a K8s node counts as enrolled while it hosts ≥1 live Ray worker pod (`warm`); `cold`/scaled-to-zero nodes do not count. Cap is checked at deploy time only, never evicts a running deployment. (Call: mine.) |
| Identity | API scope `fleet:admin`. Deployments reference org by numeric FK internally, UUID `public_id` externally. Int enums for status/vendor/quantization. |
| Observability | OTel logs + metrics + traces via penguin logging; vLLM and Ray Prometheus metrics scraped into the same pipeline. |

## 4. Architecture

```
                          ┌──────────────────────────────┐
  Admin UI ──► management ─┤ VllmRayBackend (shared/fleet) │
  (wizard)    (Quart)      │  inventory · fit · placement  │
                           └───────┬──────────────┬───────┘
                                   │ K8s API      │ Ray Serve REST (head:8265, ns-internal)
                                   ▼              ▼
   KubeRay operator (subchart) ─► RayCluster "waddleai-inference"
        head (General node, no GPU)
        worker group  nvidia-rtx4090  image: vllm-cuda   resource nvidia.com/rtx4090
        worker group  nvidia-rtx4070  image: vllm-cuda   resource nvidia.com/rtx4070
        worker group  amd-<class>     image: vllm-rocm   resource amd.com/gpu
        worker group  intel-<class>   image: vllm-xpu    resource gpu.intel.com/i915|xe
                                   │
   Ray Serve LLM app per deployment (route_prefix /d/<slug>) ─► OpenAI-compatible
                                   │
   proxy ◄── connection_links row (provider=vllm, url=http://<head-svc>:8000/d/<slug>/v1)
```

### 4.1 Ray cluster (one per WaddleAI install)

- **KubeRay operator** as a pinned Helm subchart in `k8s/helm/waddleai/charts/`,
  installed when `fleet.vllmRay.enabled=true`.
- **One `RayCluster`** named `waddleai-inference` in the product namespace,
  rendered from values. Head pod on General nodes (nodeAffinity away from
  GPU nodes), no GPU, holds Serve controller + dashboard.
- **One worker group per (vendor, gpu-class)** present in the cluster,
  rendered from `fleet.vllmRay.workerGroups[]`. Each group: image for that
  vendor, device-plugin resource request for that class, nodeSelector on
  the class label, `minReplicas: 0`, `maxReplicas` = number of matching
  GPUs / GPUs-per-worker. Ray autoscaler scales groups to zero when idle;
  the existing `IdleTeardownController` is *not* used for Ray workers (Ray
  owns that lifecycle); it is used only for deployment-level teardown.
- Worker pods advertise Ray custom resources:
  `gpu_vendor:nvidia`, `gpu_class:nvidia-rtx4090`, and `vram_mb` (node
  total). Ray's native `GPU` resource remains the count.

### 4.2 Making mixed GPUs schedulable (the key mechanism)

Kubernetes device plugins hand out GPUs arbitrarily; a pod requesting
`nvidia.com/gpu: 2` on a 4090+4070 node may get either. Therefore:

- **NVIDIA:** the NVIDIA device plugin (via the already-vendored
  `gpu-operator` subchart) is configured with per-device-name resource
  renaming (`resources.gpus[]: {pattern: "*RTX 4090*", name: rtx4090}`),
  so each GPU model is a distinct extended resource
  (`nvidia.com/rtx4090`, `nvidia.com/rtx4070`). The class list is a Helm
  value; the inventory job reports any NVIDIA GPU whose name matches no
  configured class as "unclassified, not schedulable" in the UI.
  **Co-tenancy rule (owner decision):** no GPU workloads run on AI nodes
  except what WaddleAI schedules. Renaming therefore applies only when
  `fleet.vllmRay.enabled`, and WaddleAI's own Ollama / llama.cpp fleet
  deployments must stay compatible: the chart renders their GPU resource
  request from the node's class label (`nvidia.com/<class>`) instead of
  `nvidia.com/gpu` when renaming is active, and the fleet API refuses an
  Ollama / llama.cpp deployment targeting a renamed AI node with a plain
  `nvidia.com/gpu` request, stating the fix. Admins' own non-WaddleAI GPU
  pods are out of scope and documented as unsupported on AI nodes.
- **AMD / Intel:** their plugins expose one resource name per vendor.
  v1 assumes **homogeneous GPU models per AMD/Intel node**; the inventory
  job flags a mixed AMD/Intel node as `mixed_unsupported` and the UI shows
  it as not schedulable rather than guessing. (Decision: mine. Revisit
  when the AMD GPU operator's per-model resources are stable.)
- **Node labels:** `waddleai.penguintech.io/gpu-vendor`, `/gpu-classes`
  (comma list), `/ai-node=true`. Set by a Helm-rendered Node Feature
  Discovery rule (NFD is part of gpu-operator) where possible, otherwise by
  the inventory job (below) via the K8s API. Admins may pre-label.

### 4.3 GPU inventory

- A **Ray task fan-out** (one per live worker, triggered by the management
  service every 60 s and on demand) queries the vendor tool in-pod
  (`nvidia-smi --query-gpu`, `rocm-smi --showmeminfo`, `xpu-smi`) and
  returns per-GPU: index, vendor, product name, class, VRAM total/used,
  PCI bus id. Results land in table `gpu_inventory` and are the source for
  `NodeInfo.vram_total_mb/vram_free_mb` (MB, per existing contract) and a
  new `NodeInfo.gpu_class` + `gpu_vendor` field.
- Nodes with zero live workers (group scaled to zero) are inventoried from
  node allocatable resources + labels, marked `cold`.

### 4.4 Fit calculator and layout recommendation

Runs server-side, pure function, unit-tested, called by the UI before
deploy and enforced again at deploy:

- Weight bytes = params × bytes/param (bf16 2, fp8 1, int4/AWQ/GPTQ 0.5 +
  overhead factor), + KV-cache headroom (configurable %, default 20%) +
  CUDA/ROCm context (~1.5 GB/GPU).
- Candidate layouts enumerated over available (vendor, class) pools:
  `replicas × tp × pp`, TP only within a node, PP only across nodes of the
  same class. Ranked: fits on one node > fewer nodes > more replicas.
- Output is plain-language: "Fits on 1 node using 2× RTX 4090 (46 GB of
  48 GB). Recommended." / "Does not fit in any pool. Options: use 4-bit
  quantization (needs 68 GB), or span 3 nodes of RTX 4090." Numbers come
  from the registry's parameter count, or Hugging Face `config.json` via
  the existing digest-verified artifact path.
- Params/size unknown → the UI asks the admin for the parameter count;
  never guesses.

### 4.5 Deploying a model

1. Admin picks model (registry entry or HF repo id), quantization (default
   = recommended), layout (default = recommended), vendor/class pool,
   replicas, max context. Deny-list check (origin) first; Free-tier caps.
2. Management writes a `vllm_deployments` row (`status=pending`) and
   ensures the weights are in the model store (§6.4) via the existing
   `shared/fleet/huggingface.py` downloader (digest-verified, one pull per
   cluster). Status `pulling` with byte progress.
3. Management builds a **Ray Serve LLM** application config
   (`ray.serve.llm` `LLMConfig`: `model_loading_config`, `engine_kwargs`
   with `tensor_parallel_size`, `pipeline_parallel_size`, `quantization`,
   `max_model_len`, `distributed_executor_backend="ray"`;
   quantization is **pre-quantized checkpoints only** in v1 (AWQ, GPTQ,
   FP8 checkpoints) plus bitsandbytes on-the-fly on NVIDIA only; vendor
   matrix: NVIDIA awq/gptq/bnb, fp8 on Ada-or-newer; AMD awq/gptq, fp8 on
   MI300-class only; Intel bf16/fp16 only. The fit calculator refuses
   combinations outside this matrix. (Call: mine.)
   `deployment_config.autoscaling_config` for replicas; and an explicit
   **placement group spec**: single-node → `STRICT_PACK` of `tp` bundles
   each `{GPU:1, gpu_class:<c>:1}`; multi-node → `STRICT_SPREAD` of `pp`
   bundles each `{GPU:tp, gpu_class:<c>:tp}` so every stage lands whole on
   one node of the right class). Builder verifies against the pinned
   vLLM/Ray versions that bundle-to-worker mapping (`VLLM_RAY_BUNDLE_INDICES`
   or the Serve LLM equivalent) honors this; if the pinned version cannot,
   the executor stops and reports (fail loud); no alternative executor
   path is pre-planned and the class constraint is never dropped.
4. Config applied via Ray Serve REST (`PUT /api/serve/applications/`,
   multi-app, one app per deployment, `route_prefix: /d/<slug>`), reachable
   only from management pods (Cilium). Status `starting` → `warming`
   (first `/v1/models` 200 + one canary completion) → `serving`.
5. On `serving`, management creates/updates a `connection_links` row
   (`provider="vllm"`, `endpoint_url=http://<head-svc>:8000/d/<slug>/v1`,
   `model_list=[<served model name>]`) and registers the model in the
   model registry/offers as `location=local`. Proxy picks it up through
   the existing connector reload path.
6. NCCL env on all worker pods: `NCCL_SOCKET_NTHREADS=4`,
   `NCCL_NSOCKS_PERTHREAD=4`, `NCCL_SOCKET_IFNAME=<fleet.vllmRay.nccl.ifname>`
   (values-driven; default = pod primary interface) so pipeline traffic
   spreads over both LACP links. No RDMA assumed; `NCCL_IB_DISABLE=1`.

Scale / stop / delete update the Serve app and the row; delete also
removes the `connection_links` row. Teardown of weights from the store is
a separate explicit action (`DELETE /api/v1/fleet/models/<ref>/weights`,
refused while any deployment references them). Weights are pulled by a
rootless Kubernetes Job using the digest-verifying downloader; gated
repos get the org's Hugging Face token from `credentials_ref` via a
short-lived Secret.

### 4.6 Routing: augment, not replace

- Deployed models appear as `location=local` offers like Ollama models do
  today. `vllm` is added to the local-provider sets (`shared/routing/offers.py`
  and the duplicated set in `proxy/.../pipeline/stages.py`, which this work
  consolidates into one shared constant).
- The deploy wizard's last step offers "Prefer this model for…" which
  creates a `routing_rules_v2` rule (match on model alias / team / tag)
  with the commercial model as fallback. Default off; admins keep full
  control in the existing routing UI. Health-based failover uses the
  existing destination health path: an unhealthy vLLM endpoint drops out
  of offers and the fallback commercial destination serves.

### 4.7 Chargeback and quota

- Each deployment carries a **virtual price**: `price_input_per_1m` and
  `price_output_per_1m` (decimal), set in the wizard, plus a
  `comparison_model` (the commercial "provider/model" it stands in for).
  WaddleAI's pricing is the global `token_conversion_rates` table
  (per provider+model; there is no per-org price store). Its rates are
  divisors (LLM tokens per WaddleAI credit) and `base_cost_per_waddleai_token`
  is USD per credit, so a USD price converts as
  `input_rate = base_cost_per_waddleai_token × 1e6 / price_input_per_1m`
  (same for output). On create or price change the deployment upserts its
  own row (`provider="vllm"`), so the quota engine charges it through the
  unchanged existing lookup.
- **Default price = true cost of ownership (owner decision 2026-11-05),
  not a discount off commercial; admins can always override.**
  `price_mode` is `AUTO_TCO` (default) or `MANUAL`. In AUTO:

  ```
  node_base_usd   = cpu_usd + ram_gb × ram_usd_per_gb + nvme_gb × (nvme_pro ? nvme_pro_usd_per_gb : nvme_usd_per_gb) + network_devices × network_device_usd
  gpu_hourly      = Σ over GPUs in the layout of (card_usd / lifecycle_hours + tdp_watts/1000 × kwh_usd)
  node_hourly     = nodes_used × (gpus_used_on_node / gpus_on_node) × (node_base_usd / lifecycle_hours + node_base_watts/1000 × kwh_usd)
  hourly_cost     = gpu_hourly + node_hourly
  tokens_per_hour = tokens_per_s × 3600 × utilization_pct/100      # tokens_per_s = observed (rolling 24h p50 output tok/s) once ≥1h served, else per-class estimate × model-size factor
  output_per_1m   = hourly_cost / tokens_per_hour × 1e6
  input_per_1m    = output_per_1m × input_ratio                     # prefill is cheaper; default 0.2
  ```
  Owner-supplied defaults (2026-11-05): `kwh_usd = 0.18`,
  `ram_usd_per_gb = 1`, `nvme_usd_per_gb = 1` (standard),
  `nvme_pro_usd_per_gb = 2` (PRO / PCIe 5 / high-throughput),
  `network_device_usd = 400` per device (each NIC and the switch port
  share count as devices), `lifecycle_years = 10`. Mine:
  `utilization_pct = 50`, `input_ratio = 0.2`. Per GPU class:
  `card_usd`, `tdp_watts`, `est_tokens_per_s`; per node class: `cpu_usd`,
  `ram_gb`, `nvme_gb`, `nvme_pro`, `network_devices`, `node_base_watts`,
  `gpus_per_node` (or an explicit `node_base_usd` override). `card_usd`
  and `tdp_watts` default from a **static GPU price reference** (average
  street price, not MSRP, for every discrete NVIDIA/AMD/Intel GPU
  released since 2018, dated and committed with sources at
  `docs/reference/gpu-price-reference-{nvidia,amd,intel}.json`);
  an unknown class with no explicit values is refused, never guessed. **Admin
  override at every level:** all inputs editable in the backend settings
  UI, and any deployment can switch to MANUAL with an explicit price.
  AUTO prices are recomputed daily from observed throughput; each
  recomputation writes a deployment event, and usage rows keep their
  snapshot price. The wizard shows the breakdown (card amortization,
  power, ecosystem share, assumed throughput) next to the price.
- **The same formula prices every locally hosted model (owner decision
  2026-11-05).** Ollama and llama.cpp fleet deployments carry the same
  `price_mode`, prices, breakdown, daily recompute, rate-row upsert and
  usage snapshot. Their GPU set comes from the node's class label and the
  deployment's GPU resource request, with VRAM/TDP/price from the static
  reference, since those backends have no Ray workers to run the
  collector. Chargeback and savings reports cover all local kinds
  (`local_deployment_kind` + `local_deployment_id` on usage rows).
- The proxy's `vllm` connector reports real token counts from the
  OpenAI-compatible `usage` object on every response, including the final
  streamed chunk (`stream_options.include_usage=true`), never a tokenizer
  estimate. Metering flows through the existing per-destination usage
  path and the elastic pooled quota debits the team/org budget at the
  virtual price exactly as it does for commercial destinations.
- Quota exhaustion behaves identically for local and commercial
  destinations (existing hard-block + upgrade/top-up path). Routing
  "prefer local" never skips the quota check.
- Usage records carry `local_deployment_kind` + `local_deployment_id` so
  chargeback reports can be split per deployment as well as per team/org. The savings card is then
  (commercial equivalent cost − virtual cost) over the same records.
- Price changes (manual or daily AUTO recompute) apply to new requests
  only; usage records store the price used at request time.

### 4.8 Multi-node llama.cpp via RPC (and Ollama where supported)

vLLM on Ray is the preferred multi-node engine, but the existing
**llama.cpp fleet deployment gains a first-class multi-node shape built
on llama.cpp's RPC backend (owner decision 2026-11-05):** one
`llama-server` pod (the "head", holding the model file and serving the
OpenAI-compatible API) plus N `rpc-server` worker pods on other GPU
nodes, with `--rpc host:port,...` and `--tensor-split` distributing
layers across the head's local GPUs and each remote worker. Same
placement rules as Ray: workers in one deployment are one vendor; a
class mix is allowed only with `allow_mixed_class` and the split is
VRAM-proportional. Same fit calculator (weights + KV cache across the
summed VRAM), same TCO pricing, same chargeback, same wizard with an
"Engine" choice (vLLM on Ray — recommended; llama.cpp RPC — "slower
across nodes, use when you need GGUF or llama.cpp-only features").

- Worker pods are a per-deployment StatefulSet-like set rendered by the
  llama.cpp manager (one pod per (node, GPU set)), not a DaemonSet, so
  the head's `--rpc` list is stable; the head waits for all workers'
  readiness before loading.
- **RPC has no auth or TLS**: workers are reachable only from their
  head pod via a per-deployment CiliumNetworkPolicy (pod-label scoped);
  never cross-namespace, never via Service of type other than ClusterIP.
- Weights load on the head from the shared model store; RPC workers hold
  only their layer slices in VRAM (no local model copy needed).
- Vendor images: the existing llama.cpp images gain an `rpc-server`
  entrypoint variant per vendor (CUDA, ROCm, SYCL for Intel); mixing
  vendors inside one RPC deployment is refused.
- Telemetry: per-deployment worker-up gauge, layer split recorded in
  the deployment row, load-time histogram shared with vLLM.
- Ollama multi-node: included **only if** Ollama natively exposes the
  RPC backend in the pinned version; otherwise the UI states "Ollama
  runs single-node; use llama.cpp RPC or vLLM on Ray for multi-node" and
  no fork is vendored. (Verified during planning; result recorded in
  the plan.)

### 4.9 Vendor images

Three images built by CI, pinned by digest, rootless (`USER 1000`):

| Image | Base | Notes |
|---|---|---|
| `waddleai/vllm-ray-cuda` | `rayproject/ray-llm:<ray>-py312-cu<ver>` (Ray + vLLM bundled) | NVIDIA; verify non-root |
| `waddleai/vllm-ray-rocm` | `rocm/vllm:<ver>` + pinned `ray[serve,llm]` | AMD |
| `waddleai/vllm-ray-xpu` | vLLM XPU wheel on Intel oneAPI base + pinned `ray[serve,llm]` | Intel, single-GPU only |

Ray head uses `rayproject/ray:<ray>-py312` (CPU). Python 3.12 across all Ray images (upstream tags), independent of the control plane's 3.13. Ray version must be
identical across head and all workers.

## 5. Surfaces

### 5.1 UI (React, `webui`) — "GPU Fleet" section, flag-gated

| Page | Content |
|---|---|
| Overview | Node cards grouped by vendor/class: GPUs, VRAM used/free bar, what's loaded, cold/warm/unschedulable badges, inventory age ("updated 30s ago"). Savings card: tokens + est. cost served locally vs. commercial, last 7/30 days (from existing usage metering). |
| Deployments | Table: model, pool, layout (plain: "1 node · 2 GPUs"), replicas, status with step timeline, p50 latency, tokens/min, actions (scale, stop, delete, logs link). |
| Deploy wizard | 1 Pick model → 2 Fit check (live, plain-language, recommended/alternatives) → 3 Advanced (collapsed: quantization, TP/PP, max context, allow-mixed-class) → 4 Chargeback (AUTO cost-of-ownership price with breakdown: card amortization, power, ecosystem share, assumed throughput; "Compare against" commercial model; "Set price manually" override; plain explanation of who gets billed) → 5 Routing ("make available via proxy" on by default; "prefer for…" optional) → 6 Review & deploy. |
| Backend settings | The `vllm_ray` fleet backend row: Ray cluster status, worker groups, model store usage, hot-cache toggle state. |

Status/error copy is written for non-AI admins; every refusal states
what to change.

### 5.2 API (`services/management`, scope `fleet:admin`, both flags)

| Method · path | Purpose |
|---|---|
| `POST /api/v1/fleet/backends` type `vllm_ray` | Register the backend (existing endpoint; type added to allowlist; default scope `full_lifecycle`) |
| `GET /api/v1/fleet/gpu-inventory` | Inventory with filters: vendor, class, node, schedulable |
| `POST /api/v1/fleet/vllm/fit-check` | Fit calculator, no side effects |
| `GET/POST /api/v1/fleet/vllm/deployments` | List (filters: status, vendor, model; pagination) / create |
| `GET/PATCH/DELETE /api/v1/fleet/vllm/deployments/<uuid>` | Detail with status timeline / scale, stop, resume / delete |
| `GET /api/v1/fleet/vllm/savings` | Local vs commercial token/cost rollup |
| `GET /api/v1/fleet/vllm/chargeback` | Per team/org/deployment virtual-cost rollup; filters: period, team, deployment |

Responses are explicit DTOs (`@validate_response`), OpenAPI updated
(`openapi/v1.yaml`). API filter coverage tests per `testing.md`.

### 5.3 CLI

`waddleai-cli fleet vllm {inventory,fit,deploy,list,scale,stop,delete}`
thin wrappers over the API. Lower priority than UI; may land in a
follow-on PR within the same release.

## 6. Data, Helm, security

### 6.1 Schema (migration 028)

- `gpu_inventory`: id, org_id FK, fleet_backend_id FK, node_uid, node_name,
  gpu_index, vendor (int enum: 1 nvidia, 2 amd, 3 intel), gpu_class
  (string, normalized e.g. `nvidia-rtx4090`), product_name, vram_total_mb,
  vram_used_mb, pci_bus_id, schedulable (bool), state (int enum: warm,
  cold, unclassified, mixed_unsupported), last_seen. Unique
  (fleet_backend_id, node_uid, gpu_index).
- `vllm_deployments`: id, public_id UUID, org_id FK, fleet_backend_id FK,
  name, model_ref, served_model_name, vendor (int enum), gpu_class,
  allow_mixed_class (bool), quantization (int enum: none, awq, gptq, fp8,
  bnb), dtype, tensor_parallel, pipeline_parallel, replicas_min/max,
  max_model_len, status (int enum: pending, pulling, starting, warming,
  serving, degraded, stopped, failed), status_detail, serve_app_name,
  endpoint_url, connection_link_id FK nullable, price_mode (int enum:
  1 auto_tco, 2 manual), price_input_per_1m, price_output_per_1m
  (NUMERIC(12,6), not null), price_breakdown JSON (last TCO inputs and
  outputs), observed_tokens_per_s NUMERIC(10,2) null, created_at,
  updated_at.
  Usage/metering records gain nullable `local_deployment_kind` (int:
  1 vllm, 2 ollama, 3 llamacpp), `local_deployment_id`, and
  `unit_price_input/output` snapshot columns. `ollama_deployments` and
  `llamacpp_deployments` gain the same pricing columns as
  `vllm_deployments`.
  Unique (org_id, name).
- `vllm_deployment_events`: deployment_id FK, ts, state (int), message —
  drives the UI timeline.
- `BackendType.VLLM_RAY = "vllm_ray"`; registry `_MODULE_MAP` entry →
  `shared/fleet/vllm_ray/` (package: backend, inventory, fit, serve_config, ray_client).

### 6.2 Helm values (`fleet.vllmRay.*`)

`enabled`, `ray.version`, `images.{cuda,rocm,xpu}` (digest-pinned),
`head.{resources,nodeSelector}`, `workerGroups[]` (`name, vendor, gpuClass,
resourceName, gpusPerWorker, maxReplicas, nodeSelector, tolerations,
image`), `nvidiaClassMap[]` (pattern→name for the device plugin config),
`modelStore.{storageClass,size,accessMode}`, `hotCache.{enabled,hostPath}`,
`nccl.ifname`, `serve.port` (8000), `dashboard.port` (8265),
`tco.{kwhUsd: 0.18, lifecycleYears: 10, utilizationPct: 50, inputRatio: 0.2, ramUsdPerGb: 1, nvmeUsdPerGb: 1, nvmeProUsdPerGb: 2, networkDeviceUsd: 400}`,
`workerGroups[].cost.{cardUsd, tdpWatts, estTokensPerS, cpuUsd, ramGb, nvmeGb, nvmePro, networkDevices, nodeBaseWatts, gpusPerNode, nodeBaseUsd?}`.
Ray ports (8000/8265/6379/10001 + worker range) get their own
CiliumNetworkPolicies scoped to management→head and head↔workers (not
added to `topology.fleetPorts`, which would open them on Ollama pods).

### 6.3 Network / runtime security

- CiliumNetworkPolicy: Ray head ports (6379 GCS, 8265 dashboard/Serve API,
  10001 client, 8000 Serve HTTP) reachable only from management pods and
  Ray worker pods; workers' Ray ports (pinned worker port range) only from
  head/workers. Ray gets its own CNPs rather than extending
  `topology.fleetPorts`, which would open Ray ports on Ollama pods.
  Nothing exposed via Ingress. Proxy reaches head:8000 only.
- **Authentication, layered (SPIFFE-ready per `security.md`). Owner
  decision 2026-11-05: SPIFFE preferred, but bearer/JWT authentication
  is MANDATORY on every inference endpoint regardless, assuming a bad
  actor can reach the inference network. There is no unauthenticated
  mode; a link that cannot be authenticated is refused, not warned.**
  1. *Workload identity on every link* — Cilium mutual authentication
     (`authentication.mode: required` on the fleet CiliumNetworkPolicies,
     SPIRE-issued SPIFFE IDs under `spiffe://penguintech.io/<env>/waddleai-<component>`)
     for proxy→head, head↔Ray workers, llama-server→rpc-server, plus
     Cilium WireGuard encryption (mutual auth authenticates but does not
     encrypt). This is what secures `rpc-server` and Ray's GCS/object
     store, which have no auth of their own. Values toggle
     `fleet.security.mutualAuth` (default on). When it is off: Ray
     inter-node links (GCS/object store) must be covered by step 3's
     internal TLS with cluster-issued certs, else vLLM multi-node layouts
     are refused with "enable Cilium mutual auth (SPIRE) or Ray internal
     TLS to deploy across nodes"; **llama.cpp RPC has no TLS of its own,
     so RPC multi-node requires Cilium mutual auth specifically** and is
     refused otherwise with "enable Cilium mutual auth (SPIRE) to deploy
     llama.cpp across nodes". Fail closed, never fall back to open.
  2. *Bearer tokens at every OpenAI-compatible endpoint, always on* — a
     per-deployment token minted by WaddleAI (≥256-bit random, or a
     short-lived machine JWT where the engine can validate one), stored
     encrypted on the connection link (`api_key`), rotated on every
     redeploy and on demand from the UI; the proxy is the only holder.
     Engines are started with the token enforced or they are not
     started: vLLM `--api-key` /
     `VLLM_API_KEY` (for Ray Serve LLM, enforced by an auth middleware in
     the Serve app if the pinned version lacks native support — verified
     in planning), `llama-server --api-key-file` from a Secret mount.
  3. *Ray control plane and mesh* — Ray token auth for dashboard/jobs/
     Serve REST is **required**: the pinned Ray version must support it
     (a version without it is not an allowed pin), token from a Secret;
     Ray internal TLS (`RAY_USE_TLS=1`) using SPIRE SVIDs when available,
     else cert-manager-issued per-cluster certs (`fleet.vllmRay.rayInternalTls`,
     default on; SVID/cert TTL and reload behaviour recorded in the plan).
  5. *Key lifecycle (owner decision 2026-11-05)*: every key or token an
     operator or admin needs is **generated by the Helm chart at install**
     (Ray auth token, internal-TLS CA/certs via cert-manager, bootstrap
     secrets), never typed into values and never regenerated on upgrade
     (a `pre-install` hook / `lookup` keeps existing Secrets), and is
     **rotatable through the API** (`POST /api/v1/fleet/secrets/<name>/rotate`
     for cluster-level keys, `POST .../deployments/<uuid>/rotate-token`
     for per-deployment keys), with the UI and CLI calling those
     endpoints. Rotation of the Ray token or certs performs a rolling
     restart of head and workers (Ray reads them once at startup) and
     records an audit event; values of keys are never returned by the
     API, only fingerprints and rotation timestamps. **Externally managed
     keys (owner decision 2026-11-05):** every key the chart would
     generate can instead be supplied by the operator through an
     environment variable or an existing Secret reference (Docker and
     Kubernetes alike), so it can be injected from Infisical, AWS Secrets
     Manager, Vault, External Secrets Operator, etc. Precedence: explicit
     env var > `existingSecret` reference in values > chart-generated. The
     env names are fixed and documented (`WADDLEAI_RAY_AUTH_TOKEN`,
     `WADDLEAI_RAY_TLS_CERT_PATH` / `_KEY_PATH` / `_CA_PATH`,
     `WADDLEAI_FLEET_TOKEN_ENCRYPTION_KEY` for the connection-link
     encryption key, `WADDLEAI_SPIFFE_TRUST_DOMAIN`). An externally
     managed key is shown in the UI as "managed externally" and the
     rotate action is disabled for it with that reason, since rotation
     then happens in the external manager; the API still reports its
     fingerprint and last-seen change.
  6. *Testable*: every endpoint has a regression test that an
     unauthenticated request is rejected (401/403, or TCP refused by
     policy for RPC), counted in the telemetry gate.
  4. The Ray dashboard and Serve REST are never exposed via Ingress;
     network policy applies regardless of the above.
- Tetragon TracingPolicy allowlists `ray`, `python`, vendor SMI binaries
  in the worker images.
- Worker pods: `runAsNonRoot`, no privilege; GPU access solely via the
  device plugin resource. No secrets in the Ray config; HF tokens for
  gated models come from the org's encrypted `credentials_ref` and are
  injected as a K8s Secret env, never logged.

### 6.4 Model store and hot cache

Migration number: 027 is taken by the untracked configuration-as-code
migration; this feature uses the next free number at merge time.

- **Model store (default, PSA-restricted compliant):** one RWX PVC from
  the cluster's shared storage class (the Storage nodes), mounted on all
  workers at `/models`, `HF_HOME` pointed there. One digest-verified pull
  per cluster. Expect ~1–3 min cold load for a 4-bit 120B from HDD-backed
  storage; shown in the UI as the `starting` step.
- **NVMe hot cache (opt-in):** `hostPath` on AI nodes (e.g.
  `/var/lib/waddleai/models`), with the Ray worker pods carrying a
  documented `# STATEFUL EXCEPTION (approved)` and the namespace PSA
  exception required for `hostPath`. Workers copy from the store to the
  hot cache on first load. Owner-approved opt-in (§8). PSA mechanism:
  Kubernetes PSA cannot exempt individual pods, so when
  `hotCache.enabled=true` the chart sets the product namespace to
  `enforce=baseline` (warn/audit stay `restricted`); if
  `hotCache.kyvernoPolicyException=true` it instead renders a Kyverno
  PolicyException scoped to the Ray worker pods and keeps
  `enforce=restricted`. Default off keeps the namespace fully restricted.

## 7. Observability and tests

- Metrics: `waddleai_gpu_vram_total/used_mb{vendor,class,node}`,
  `waddleai_vllm_deployments{status}`, `waddleai_vllm_placement_decisions_total{outcome}`,
  `waddleai_vllm_model_load_seconds` histogram, plus vLLM's own
  `/metrics` (TTFT, throughput, KV usage) and Ray metrics scraped into the
  OTel collector. Logs via penguin logging; traces across
  management → Ray Serve REST → proxy → vLLM.
- Tests: unit (fit calculator table-driven incl. 12B/70B/120B cases and
  every refusal; placement with class constraints; inventory parsing for
  all three vendor tools from fixture output), registry/base/conformance
  updates, API filter coverage, integration against a fake Ray Serve REST
  + fake SMI (asserting the degraded sentinel, never passing on a dead
  endpoint), Playwright e2e for the wizard behind the flag, Helm render
  tests for every worker-group permutation. **Real-path validation:** on
  dal2beta only (single-node TP, multi-node PP, mixed-class refusal,
  RPC 2-node, auth rejection, quota debit), recorded with counts in the
  release PR; see the master plan §E.
- Screenshots in `docs/screenshots/` for the new pages; docs page under
  `docs/` for admins written at the same audience level as the UI.
- **Validation hardware (owner):** dal2beta has 3 AI nodes and 2 storage
  nodes now; 2 compute nodes arrive about 2026-11-13. Release gate: real
  single-node TP, real multi-node PP across ≥2 AI nodes, and the
  mixed-class refusal on a mixed node, all on dal2beta, counts reported in
  the PR. Until compute nodes exist the Ray head's nodeSelector is a
  values override so it can run on a storage node (CPU-only, fine).

## 8. Owner decisions (2026-11-05)

1. **NVMe hot cache via `hostPath`:** approved as opt-in, off by default,
   shipped in v1 with the documented `# STATEFUL EXCEPTION (approved)` and
   the namespace PSA exception scoped to Ray worker pods only.
2. **Savings card:** priced from the deployment's `comparison_model`
   rate in `token_conversion_rates` (the deployment's configured rates,
   which is what "actual" means in this codebase), never an external list
   price.
6. **Default virtual price = amortized true cost** (10-year lifecycle:
   GPU card + regional power at $0.18/kWh + node ecosystem share at $1/GB
   RAM, $1/GB standard NVMe, $2/GB PRO NVMe, $400 per network device),
   admin-overridable per input and per deployment; see §4.7.
7. **Org isolation of destinations:** `connection_links` has no org
   column today; migration adds nullable `owner_org_id`, vLLM links are
   org-owned, and offer/routing selection never picks a foreign org's
   link.
3. **GPU co-tenancy:** AI nodes host only WaddleAI-scheduled GPU work; see
   §4.2 co-tenancy rule.
4. **Validation:** dal2beta, see §7.
5. **Chargeback:** virtual token budgets apply to self-hosted models; see
   §4.7.

## 9. Left to the builder

- Exact pinned versions of Ray, vLLM, KubeRay, and the three images, and
  verification of placement-group → worker mapping in those versions.
- Normalization rules for GPU class names across vendors.
- Fit-calculator overhead constants (validated against real loads).
- Serve autoscaling parameters; inventory refresh cadence.
- Internal module layout within `shared/fleet/vllm_ray/` (package: backend, inventory, fit, serve_config, ray_client) and the
  management service; whether CLI lands in the same PR.
