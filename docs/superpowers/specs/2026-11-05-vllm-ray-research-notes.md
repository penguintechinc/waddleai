# RPC / multi-node inference research (2026-11-05)

Method: Gemini grounded research only (not independently fetched). Sources are the grounding-redirect URLs returned, plus canonical URLs the research named. Treat version numbers and CVE/issue numbers as UNVERIFIED until checked against the repos.

## 1. Ollama multi-node
- No native multi-host inference in upstream ollama/ollama. Single-host multi-GPU only.
- `OLLAMA_RPC` / `--rpc` do NOT exist upstream (research states they are fork-only or bypass llama-server).
- Forks/tools named: borism/ollama-cluster (LAN autodiscovery + RPC spillover), K2/olol (gRPC LB, layer sharding), SOLLOL, exo-explore/exo (P2P, native tensor split).
- Upstream issues (research-cited): #4643, #5983, #9147 (maintainers confirmed no multi-node), #1188, #2319, #16550.
- Sources: https://github.com/ollama/ollama , https://ollama.com/blog/new-model-scheduler , https://github.com/borism/ollama-cluster , https://github.com/K2/olol , https://github.com/exo-explore/exo

## 2. llama.cpp RPC backend
- Build: `-DGGML_RPC=ON`. Source: https://github.com/ggml-org/llama.cpp/blob/master/tools/rpc/README.md
- rpc-server flags: `-H/--host` (default 127.0.0.1), `-p/--port` (default 50052), `-d/--device` (e.g. CUDA0, MTL0, SYCL0, Vulkan0), `-m/--mem` (MB cap advertised), `-c/--cache` (worker-side weight cache, ~/.cache/llama.cpp/rpc), `-t/--threads`.
- Coordinator: `llama-server --rpc host:port,host:port` (env `LLAMA_ARG_RPC`). Each endpoint becomes a GGML device.
- --tensor-split: works with layer split (default `--split-mode layer`). Row/tensor split over RPC is discouraged (AllReduce per matmul).
- Mixed backends (CUDA/Metal/ROCm/SYCL/CPU): research says yes; REQUIRES identical llama.cpp commit on all nodes (no wire version negotiation). Unverified against the code.
- Auth/TLS: none. Protocol deserializes graphs from raw TCP. Upstream warning: never expose on open networks. Use WireGuard/Tailscale/SSH tunnels or isolated networks.
- Throughput: capacity play, not speed. Decode penalty ~20-50% on 10GbE, ~60-85% on 1GbE, ~10-20% on RDMA/TB5 (research estimates, not benchmarked).

## 3. Kubernetes pattern
- Pattern: 1 llama-server leader + N rpc-server workers, LeaderWorkerSet (kubernetes-sigs/lws) for group lifecycle. Source: https://github.com/kubernetes-sigs/lws
- Images shipping rpc-server:

| Image | Maintainer | Notes |
|---|---|---|
| ghcr.io/ggml-org/llama.cpp (full, full-cuda, server-cuda, server-rocm) | ggml-org | official; full builds include ggml-rpc-server (verify) |
| public.ecr.aws/deep-learning-containers/llama-cpp | AWS DLC | includes rpc-server, CUDA |
| docker.io/evilfreelancer/llama.cpp-rpc | community | rpc-server + llama-server |
| docker.io/ezforever/llama.cpp-static | community | static rpc-server binaries |
| ghcr.io/defilantech/llmkube-runtimes | LLMKube | RPC sharding runtimes; operator GPUShardingSpec |

- Operator: LLMKube (https://llmkube.dev). Helm: none confirmed official.
- vs vLLM+Ray: llama.cpp RPC = memory pooling over commodity Ethernet, heterogeneous HW, GGUF. vLLM/Ray = throughput, needs homogeneous GPUs + fast interconnect (NCCL/RoCE/IB).

## 4. Ray authentication
- Introduced: Ray 2.52.0, `RAY_AUTH_MODE=token` (opt-in).
- Default-on: 2.59.0 for local `ray.init()`; 2.61.0 for all clusters incl. KubeRay. Opt-out `RAY_AUTH_MODE=disabled`.
- Covers: GCS/raylet/internal gRPC, Jobs API, Dashboard, Serve management REST (/api/serve/applications/) via dashboard middleware (401 without Bearer).
- Does NOT cover: Serve HTTP ingress (user endpoints, port 8000). Needs in-app FastAPI auth or gateway.
- Token supply precedence: `RAY_AUTH_TOKEN` (env) > `RAY_AUTH_TOKEN_PATH` (file) > `~/.ray/auth_token`. KubeRay 1.6.0+ injects via Secret.
- Internal TLS envs: `RAY_USE_TLS=1`, `RAY_TLS_SERVER_CERT`, `RAY_TLS_SERVER_KEY`, `RAY_TLS_CA_CERT`. Certs read ONCE at process start; no reload. Rotated SPIRE SVIDs require process/pod restart. Tracked: ray-project/ray#65365 (in-process TLS reload). Default-token change: #67047.
- Sources: https://docs.ray.io/en/latest/ray-security/token-auth.html , https://docs.ray.io/en/latest/ray-core/configure-tls.html , https://github.com/ray-project/ray/issues/65365

## 5. Auth/TLS on serving + Cilium
- ray.serve.llm: no native API key/bearer option in LLMConfig or build_openai_app (VLLM_API_KEY ignored). Add FastAPI middleware around the app or an API gateway. Issue: ray-project/ray#59578.
- llama-server: `--api-key KEY` (env LLAMA_API_KEY, comma list allowed), `--api-key-file` (env LLAMA_ARG_API_KEY_FILE, `#` comments), `--ssl-cert-file` / `--ssl-key-file` (env LLAMA_ARG_SSL_*). SSL needs build with `-DLLAMA_OPENSSL=ON`. Source: https://github.com/ggml-org/llama.cpp/tree/master/tools/server
- rpc-server: still no auth or TLS per research. Cited CVEs CVE-2026-34159 and CVE-2026-78147 are UNVERIFIED.
- Cilium mutual auth: `authentication.mode: required` in CiliumNetworkPolicy, SPIFFE/SPIRE-backed, introduced Cilium 1.14, still Beta per research (check current docs). Sources: https://docs.cilium.io/en/stable/network/servicemesh/mutual-authentication/ , https://github.com/cilium/cilium/blob/main/design-cfps/cilium/CFP-22215-mutual-auth-for-service-mesh.md
- Auth is out-of-band; payload NOT encrypted by it. Payload encryption needs `encryption.type: wireguard` or `ipsec`.

## Caveats
- Gemini cited grounding-redirect URLs, not direct sources. Not fetched by me.
- Ollama multi-node and rpc-server cluster behavior were not confirmed in code.
