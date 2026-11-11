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

## Exec enforcement details

- Policies hook `security_bprm_check` (the resolved binary of every `execve`, `execveat`/`fexecve`
  and `#!` interpreter), not `sys_execve` arg0, which `execveat` bypasses. Allowlists therefore hold
  REAL paths (e.g. `/usr/local/bin/python3.13`, not the `python3` symlink).
- Ray pods start without a shell: head and worker exec `/home/ray/anaconda3/bin/ray start ...`
  directly (KubeRay keeps a user-set command/args), probes are `httpGet` on the agent port, and the
  exec policy is scoped by `containerSelector` to the `ray-head`/`ray-worker` container, so KubeRay's
  bash init and autoscaler containers are untouched. `ulimit -n 65536` from KubeRay's wrapper is
  dropped (container runtime default applies).
- The Ray worker allowlist also covers nvidia-smi, ldconfig, ninja, the gcc toolchain prefixes,
  Triton's bundled backend tools (`triton/backends/nvidia/bin/`: ptxas, cuobjdump, nvdisasm; amd and
  intel backends) and `/usr/local/cuda/bin/`. Derived from the image requirements, not verified in
  the images.
- **Runbook (Ray exec/egress/read-only):** set `tetragon.model.ray.mode: observe` on alpha, run a
  real vLLM deployment (the first request compiles Triton kernels), read the `process_exec` events
  Tetragon logs for the ray pods, add the legitimate real paths to
  `tetragon.exec.components.ray-worker`, then set `enforce` (the default) before beta.
- KubeRay's `ulimit -n 65536` wrapper is dropped: Ray's raylet raises the soft NOFILE limit to the
  container's hard limit itself. `rayStartParams` stay because the in-tree autoscaler reads node
  resources from them. `--memory`/`--num-cpus` come from the container limits through the downward API.
- Classification keeps `dash`: its entrypoint is `sh -c exec hypercorn` in the main container, so it
  cannot be scoped to an init container. The app python path is the single value
  `tetragon.exec.pythonRealPath` (pinned to the Dockerfiles' base python by a test).

## Access control of the policies themselves

- `model-pod-rbac.yaml`: the manager gets `tracingpoliciesnamespaced` through a namespaced Role
  (get/list/create/update/delete) in `modelPodSecurity.networkPolicy.namespaces` only. The model
  CNPs remain under the existing cilium-reconciler ClusterRole (narrowing it needs a reconciler
  change).
- `admission-model-policy-protect.yaml`: a ValidatingAdmissionPolicy (`Deny`) stops the management
  service account updating or deleting ANY chart-owned (`managed-by=Helm`) Tetragon policy, CNP or
  CCNP, including `waddleai-default-deny`. Fleet-generated objects carry
  `managed-by=waddleai-fleet` and stay manageable. The render FAILS when modelPodSecurity is on but
  this policy cannot render (admissionPolicies off or no ValidatingAdmissionPolicy API) unless
  `modelPodSecurity.networkPolicy.protectChartPolicies.acknowledgeUnprotected=true`. The cilium
  reconciler skips Helm-owned objects instead of replacing them.
- Fleet applies are idempotent: a 409 on an object with the same owner label is replaced, anything
  else raises. A failed deploy rolls back only the policies it created.

## Limits to know

- The preset hosts are what HuggingFace and the Ollama registry redirect to today; confirm against
  a live pull when you change environments.
- Cilium enforces egress per pod, not per container. The pull-phase split (sh/curl only in the
  init container) is done by Tetragon `containerSelector` (Tetragon 1.1 or newer). The egress
  kprobe is **observe-only (Post) for Ollama and for every pull container**: a kprobe cannot match
  FQDNs, so the CNP FQDN allowlist is the only enforcement there. Ollama pulls through its own API
  in the serving container, so its egress is pod-wide and `/models` stays writable.
- `tetragon.model.privateCIDRs` (default includes 100.64.0.0/10 and the IPv6 private ranges) is
  configurable; connect() outside it is killed in serving containers.
- DNS is restricted to the pull hosts plus `*.<modelPodSecurity.networkPolicy.clusterDomain>`
  (or `dnsClusterPatterns`; default domain `cluster.local`, needed for peers and search-domain expansions). Not validated on a
  live cluster: widen it if a resolver treats the proxy's REFUSED on an expansion as fatal.
- The namespace default-deny uses an explicit `[{}]` rule (an empty list can be dropped), in the
  chart and the reconciler (a test pins them identical). Now that it enforces, `workload-network-policy.yaml`
  adds the platform's own allows: DNS, management ingress (gateway, webui, proxy), webui, proxy to
  management, management to engines, kuberay-operator. A render test asserts every workload's
  labels are selected by at least one allow policy. Provider internet egress of the proxy is still
  owned by the reconciler's FQDN policy.
- Ollama workloads are applied from the exported manifest (which carries the policies);
  `OllamaDeploymentManager.apply_policies` / `remove_policies` are the programmatic path with
  rollback and ordered teardown (Tetragon first, CNP last).
