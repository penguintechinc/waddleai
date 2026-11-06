# llama.cpp across several nodes (RPC)

One GGUF model can be served from the GPUs of several nodes: a `llama-server` **head** plus one
`rpc-server` **worker** pod per remaining GPU. It is slower than a single node (expect **20-50%
lower decode throughput**), so use it when the model does not fit on one node or you need GGUF /
llama.cpp-only features. Ollama runs single-node; use llama.cpp RPC or vLLM on Ray for multi-node.

Requires both flags `waddleai.fleet_v2` and `waddleai.fleet-vllm-ray` (default off). With the flags
off, `topology: 2` is refused (`403`).

## Creating a deployment

`POST /api/v1/llamacpp/deployments` with `topology: 2`:

| Field | Meaning |
|---|---|
| `topology` | `1` single node (default), `2` RPC multi-node |
| `gguf_bytes` | GGUF file size in bytes. Required; never guessed |
| `model_url`, `model_filename` | Where the head downloads the GGUF (https) |
| `allow_mixed_class` | Allow mixing GPU classes of one vendor (split by VRAM) |
| `rpc_workers` | Optional list of `{node_uid, gpu_class, gpu_count: 1}`: restricts planning to those GPUs |
| `price_mode`, `comparison_model` | Pricing as for every local deployment (AUTO needs one GPU class) |

The planner reads your organization's GPU inventory, picks one vendor's GPUs (VRAM-proportional
`tensor_split`), refuses with plain-language reasons (does not fit, mixed vendors/classes, fits on
one node, no inventory) and then deploys. `PATCH` re-plans a **stopped** deployment; a running one
must be stopped first. `GET /api/v1/llamacpp/deployments?topology=2&status=running` filters the list
(only your organization's deployments are listed). `GET .../health` adds `status_message`,
`workers_up` and `workers_total`.

Status timeline: `pending` -> `deploying` (starting workers k/N, loading model) -> `running` ->
`degraded` (a worker is down; routing stops) -> `deploying` (reloading model; the head is restarted
once all workers are back) -> `running`. A Kubernetes failure is `error`.

## Security

* Cilium **mutual auth is required** (`fleet.security.mutualAuth`, env `WADDLEAI_FLEET_MUTUAL_AUTH`);
  rpc-server has no authentication or TLS of its own. With it off, RPC deployments are refused:
  "enable Cilium mutual auth (SPIRE) to deploy llama.cpp across nodes".
* The worker port admits only its own head pod; the head admits only the proxy and management.
* The head enforces a per-deployment bearer token (`--api-key-file`); only that deployment's key is
  mounted. Rotate with `POST /api/v1/fleet/llamacpp/deployments/<uuid>/rotate-token` (the head
  restarts; workers are untouched). A redeploy also rotates the key.
* Kubernetes objects are named `llamacpp-<name>-<12 hex of public id>` and labelled with the
  deployment's public id, so tenants can never share or select each other's objects.

## Images

`services/llamacpp-rpc` builds `llama-server` and `rpc-server` from one llama.cpp commit
(`LLAMACPP_COMMIT`) for CUDA, ROCm and SYCL. Set the digest per vendor in
`fleet.llamacpp.images.<vendor>.digest`; an unset digest leaves that vendor unconfigured and
deployments on it fail with an error naming `WADDLEAI_LLAMACPP_RPC_IMAGE_<CUDA|ROCM|SYCL>`.
