# Self-Hosted Model Pod Hardening

Ollama, llama.cpp (single and RPC head/workers) and vLLM-on-Ray pods run on plain containers
(runc), never Kata. Two layers isolate them: **CiliumNetworkPolicy** (network) and **Tetragon**
(in-pod runtime). Both are rendered by the Helm chart and generated per deployment by the
management fleet manager (`services/management/app/services/model_pod_security.py`); the chart
passes its settings to management through `WADDLEAI_MODEL_POD_SECURITY`.

## What is enforced

| Engine | Tetragon (Sigkill by default) | CNP ingress | CNP egress |
|---|---|---|---|
| Ollama | exec allowlist `/usr/bin/ollama` (+ `/bin/ollama` for the upstream fleet image); egress kprobe logs public connects (pulls go through its API) | proxy (+ management) on 11434 | DNS (+proxy rule), Ollama registry preset |
| llama.cpp single | serve container: `llama-server` only, `/models` write-protected, connect outside private CIDRs killed; `download-model` container: `sh`/`curl` only | proxy (+ management) on 8080 | DNS, HuggingFace preset |
| llama.cpp RPC head | as above (`wait-workers` init may use `sh`/`curl`/`sleep`) | proxy (+ management) on 8080, mutual auth | DNS, own workers on 50052, HuggingFace preset |
| llama.cpp RPC worker | `rpc-server` only; connect outside private CIDRs killed | own head only on 50052, mutual auth | none |
| Ray head / worker | enforced exec allowlist, **no shell**; worker `/models` read-only; connect outside private CIDRs killed | existing Ray policies (head: proxy, management, workers; worker: head and peers) | DNS (+proxy rule) and peers only |
| vLLM weights pull Job | not covered (not a serving pod) | none | DNS, HuggingFace preset (the only vLLM pod with internet) |

Tenant isolation: fleet-generated objects select pods by the per-deployment public-id label
(`waddleai.penguintech.io/llamacpp-public-id`, `.../ollama-public-id`), never by name or numeric
id, and peers are matched on the same label, so one deployment's pods (and org's) can never be
reached by or reach another's. The proxy is the sole shared entry point.

## Values

| Key | Default | Effect |
|---|---|---|
| `modelPodSecurity.networkPolicy.enabled` | `true` | render the model-pod CNPs |
| `modelPodSecurity.networkPolicy.managementIngress` | `true` | management may call `/health`, `/api/tags`, `/api/pull`; `false` = proxy only (those calls fail) |
| `modelPodSecurity.networkPolicy.metricsPorts` | `[]` | ports the `monitoring` namespace may scrape (none of the engines expose one by default) |
| `modelPull.huggingface.enabled` | `true` | HF FQDN preset (TCP/443) for llama.cpp and the vLLM pull Job |
| `modelPull.ollama.enabled` | `true` | Ollama registry preset (TCP/443) for Ollama |
| `modelPull.extraFQDNs` | `[]` | extra hosts for every engine (private mirror) |
| `tetragon.model.enabled` / `.mode` | `true` / `enforce` | per-engine policies; `observe` logs instead of killing |
| `tetragon.model.engines.*` | see values.yaml | allowlists, read-only paths, container names |

## Limits to know

- The preset hosts are what HuggingFace and the Ollama registry redirect to today; confirm against
  a live pull when you change environments.
- Cilium enforces egress per pod, not per container. The pull-phase split (sh/curl only in the
  init container, kprobe egress only logs there) is done by Tetragon `containerSelector`
  (Tetragon 1.1 or newer). Ollama pulls through its own API in the serving container, so its
  egress is pod-wide and `/models` stays writable.
- DNS is allowed to resolve any name (needed for the DNS proxy and in-cluster peers).
- The Ray exec allowlist has no shell. KubeRay's default start wrapper is `bash -lc`; validate on
  a live cluster before relying on it and fix the RayCluster command rather than re-allowing a shell.
