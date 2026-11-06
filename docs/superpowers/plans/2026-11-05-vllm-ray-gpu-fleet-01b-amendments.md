# vLLM on Ray GPU Fleet — Amendments (authoritative)

Continuation of 01-amendments; entries here are equally authoritative.

```python
#   AM15 (owner 2026-11-05) External key injection: every chart-generated key can be supplied via env var or existingSecret (Infisical / AWS SM / Vault / ESO).
#       Precedence env var > values existingSecret > chart-generated. Fixed env names: WADDLEAI_RAY_AUTH_TOKEN, WADDLEAI_RAY_TLS_CERT_PATH/_KEY_PATH/_CA_PATH,
#       WADDLEAI_FLEET_TOKEN_ENCRYPTION_KEY (connection-link api_key encryption, falls back to the existing Fernet key used by fleet.py credentials), WADDLEAI_SPIFFE_TRUST_DOMAIN.
#       Plan A: values fleet.security.{rayAuthToken,rayTls,tokenEncryptionKey}.existingSecret + env passthrough; render tests for all three precedence levels (chart must NOT generate when existingSecret/env is set).
#       Plan C: GET /fleet/secrets reports source: "generated"|"external"; rotate on an external key → 409 {"reason":"managed externally"}; tests.
#       Plan D/CLI: "managed externally" badge, Rotate disabled with tooltip. Docs: a table of env names and an Infisical/ESO example.
#   AM16 Per-deployment key delivery to Serve replicas (closes the B5 gap): one K8s Secret `waddleai-vllm-deployment-keys` (data key = deployment slug, value = token)
#       is mounted read-only at /secrets/deployment-keys/ in EVERY Ray worker pod (Plan A RayCluster worker template; optional=true so an empty Secret is fine).
#       The Serve API-key middleware (B5) resolves the expected token for its app by reading /secrets/deployment-keys/<slug> at request time with an mtime-cached read
#       (kubelet refreshes projected Secret files within its sync period, ~1 min), compares with hmac.compare_digest, 401 otherwise.
#       Management (C2c) writes/updates the Secret key on create/rotate and REMOVES it on delete; rotation therefore needs NO Serve redeploy (drop the redeploy step),
#       and the old token stays valid until kubelet sync + grace (document ≤2 min). Tests: middleware reads from a tmp dir, rotation picks up the new file, missing file → 401 (never open).
#       llama.cpp head pods mount the same Secret with --api-key-file /secrets/deployment-keys/<slug> (Plan F F3).
#   AM17 (from Plan F review) Link-auth refusal is per engine: vLLM multi-node needs mutual auth OR Ray internal TLS; llama.cpp RPC multi-node needs mutual auth ONLY
#       (rpc-server has no TLS) — refusal string "enable Cilium mutual auth (SPIRE) to deploy llama.cpp across nodes". Plan B B7 keeps the vLLM rule; Plan F F2 the RPC rule.
#       Pricing for multi-node: apply_pricing/compute_tco take nodes_used and per-node GPU sets from the LAYOUT (vLLM pp stages, RPC head+workers), never a constant 1 — fix B4d/B6 wording; test with an RPC 1+2-node layout.
#       Plan F migration: fold F1 columns into 028 if Plan B is unmerged at the time, else 029 (executor decides at branch time).
#   AM18 llama-server loads --api-key-file once at startup (no reload) → llama.cpp rotate-token = Secret key update + rolling restart of the HEAD pod only
#       (workers untouched; in-flight requests drain via preStop); Plan F F5/F6 own it with a test; the vLLM/Ray path stays restart-free per AM16.
#       Plan C C1 DELETE /vllm/deployments/<uuid> sequence must call remove_key (C2c) before deleting the row — add the step + test.
#   AM19 (coverage review fixes)
#     a. Weight pull: new Plan C task C3b "model store pull job" — on deployment create, management runs a rootless K8s Job (image = management image) that calls
#        shared/fleet/huggingface.py's digest-verifying downloader into the RWX model store PVC (/models/<org>/<model_ref>), one pull per cluster (skip if digest present),
#        status PULLING with bytes_done/bytes_total written to vllm_deployment_events every 10 s; HF token for gated repos from the org's fleet_backends.credentials_ref (decrypted) → Job env HF_TOKEN via a short-lived Secret deleted on completion; never logged.
#        Explicit teardown: DELETE /api/v1/fleet/models/<model_ref>/weights (refuses while any deployment of any kind references it). Tests: skip-if-present, progress events, gated-token path, refusal.
#     b. Alpha smoke requirement dropped: validation happens on dal2beta only (spec §7 updated); §E is the gate.
#     c. PG-mapping fallback (custom AsyncLLMEngine Serve deployment) is NOT pre-planned: B5/B7 verify bundle→worker mapping on the pinned versions; if it fails, STOP and report (fail loud), no silent alternative.
#     d. Add tests: B7 "IdleTeardownController.sweep never deprovisions vllm_ray nodes"; B3 "cold node inventoried from allocatable+labels"; B3 fake-SMI degraded sentinel (all-zero payload → state COLD, never schedulable); A2 parametrized render matrix over worker-group permutations (nvidia-only, amd-only, intel-only, all three, mixed classes);
#        C2: every new management endpoint has a "401 without JWT / 403 without fleet:admin" case; C8 adds gauge waddleai_vllm_deployments{status}.
#     e. §E gains E10 (llama.cpp RPC 2-node completion), E11 (rpc-server reachable only from its head: connection from another pod refused), E12 (unauthenticated request to vLLM and llama-server → 401), and E6 covers an Ollama deployment's quota debit too.
#     f. Doc fixes: spec anti-goal "§2.2 origin deny-list" → "the origin deny-list (`_DENIED_ORIGINS`, shared/fleet/placement.py)"; spec §6.2 fleetPorts sentence → Ray gets its own CNPs; remove the duplicate "Task C3" stub at C1-api.md ~line 225.
#   AM20 (consistency review rulings)
#     - Rolling restart mechanism (AM13/AM16/AM18, everywhere): bump pod-template annotation waddleai.penguintech.io/restart-generation on the RayCluster head+worker templates
#       (KubeRay recreates pods on template change) / on the llama.cpp head StatefulSet; "tls-generation" and "KubeRay restart annotation" wording → this.
#     - llama.cpp API file is the EXISTING services/management/app/api/v1/llamacpp.py (no fleet_llamacpp.py).
#     - Wizard sends model_ref string (AM7 wins over AM4 wording in D1-part3); public_id is for display/selection only.
#     - WADDLEAI_VLLM_RAY_BACKEND_CONFIG is a FILE PATH to the mounted ConfigMap (AM9); not configMapKeyRef.
#     - Add to AM15 fixed env list: WADDLEAI_FLEET_MUTUAL_AUTH (true/false, set by the chart from fleet.security.mutualAuth) and WADDLEAI_FLEET_RAY_INTERNAL_TLS.
#     - Master Global Constraints: add "PSA restricted; the hot-cache opt-in sets the namespace to enforce=baseline (or a Kyverno PolicyException), spec §6.4".
#     - Images: digest pin is mandatory everywhere; delete any tag-only fallback wording (A1-helm ~line 335).
#     - C6 creates shared/quota/virtual_price.py (rate upsert + snapshot helpers) so the coverage target exists; C8 savings use comparison_model (AM1), not a per-kind table.
#     - F migration steps carry the AM17 conditional (fold into 028 if B unmerged, else 029) in EVERY step, not only the task intro; F OI-2 closed (nodes_used from layout).
#     - llama.cpp rotate-token (C1-part3 + F5): Secret key update THEN head rolling restart (AM18); remove the "unverified" open item.
#     - vLLM DELETE sequence: backend.delete → fleet_secrets.remove_key(slug) → unlink_deployment → row delete; test name includes remove_key.
#     - Pointer/header fixes: B part headers "part N of 6"; master table row A includes A1-helm-part2 (A2b) and B "part1..6"; D1/D2 context lines reflect D4→D1-part3 and D8→D2-part2; master E3 uses local_deployment_kind/id.
#     - ROCm image: pick the rocm/vllm tag whose vLLM matches the Ray-LLM bundled vLLM minor, else build vLLM ROCm from source at the pinned vLLM version (A3 states this rule; executor verifies).
#   AM21 (step-quality rulings) — symbol owners: B5 creates shared/fleet/vllm_ray/serve_app.py: build_authenticated_openai_app(llm_config:dict, slug:str, keys_dir:str="/secrets/deployment-keys") -> serve.Application;
#       B4d pricing.py also defines load_pricing_inputs(config:dict) -> tuple[TcoInputs, dict[str,GpuClassCost]] (consumed by C2/C2b/C3/F5); B6 creates tests/conformance/_fake_ray.py (FakeRayServeClient) consumed by B7/B9;
#       C8 creates shared/fleet/vllm_ray/rollup.py (savings/chargeback queries) — no "replace stubs"; C2c defines pytest fixtures fake_k8s and running_engine in tests/unit/management/conftest.py;
#       $SCRATCH = the executor session's scratchpad dir (state the literal path at execution time). Rules for every task: no `git add -A` (list files), NEVER `git stash` (repo ban),
#       commit trailer = the executing session's attribution line; test fences ≤ 40 lines (names + key assertions, no transcripts); every "Expected:" names the count or message;
#       no conditional steps ("if it doesn't exist", "tighten if weak", "fix any gap") — decide and state; placeholder shas/digests written as `<sha256: computed by executor via <command>>` with the command;
#       A6 ordered after A7; F6 "C3 starts VllmReconciler"; OI cites B4d/C2c; coverage gate = "≥ 90% on the new modules, named"; dal2beta is the validation cluster (owner statement 2026-11-05 supersedes testing.md's beta-unavailable note).
#   AM22 No stub endpoints: the /api/v1/fleet/savings and /fleet/chargeback routes (and their vllm aliases) are REGISTERED IN C8 together with rollup.py — C2 does not
#       register them returning 501 (a merged 501 route is a stub). upsert_virtual_rate lives in shared/quota/virtual_price.py (C6); B4d imports it.
#   AM23 gpu_set_for (B4d) for Ollama/llama.cpp resolves the GPU class from the LIVE pod: list pods by the deployment's label selector → spec.nodeName → node label
#       waddleai.penguintech.io/gpu-classes (first class if several); falls back to the row's node_selector when no pod is scheduled; raises PricingInputsUnavailable
#       (deployment stays AUTO with price unset and status_detail "awaiting node placement for pricing", recomputed by C3's loop) when neither exists. Test all three paths.
#       Logging: shared/fleet modules use stdlib logging today (registry.py:20); new fleet modules follow that module pattern — the penguin-logging conformance gap for shared/fleet is pre-existing and tracked separately, not widened here.
#   AM24 (security review during A execution) Hot cache = generic ephemeral volume (spec.volumes[].ephemeral.volumeClaimTemplate, storageClassName fleet.vllmRay.hotCache.storageClass,
#       e.g. a node-local NVMe class such as local-path/topolvm; size hotCache.size) on Ray worker pods. NO hostPath, NO namespace PSA downgrade, NO Kyverno PolicyException
#       (PSA baseline forbids hostPath; Kyverno cannot override built-in PSA). Namespace stays enforce=restricted always. Cache lifetime = worker pod. Supersedes the AM15/D5 hostPath text.
#       TLS/mutual-auth invariant fail() lives in an always-rendered template when fleet.vllmRay.enabled (+ render test). Shared deployment-keys Secret accepted (Ray token already grants cluster control).
#   AM26 (security review, B4d) Rate rows for local deployments are keyed per DEPLOYMENT, never by served model name: token_conversion_rates
#       (provider=<"vllm"|"ollama"|"llamacpp">, model=f"deployment/{public_id}"). upsert_virtual_rate never forces enabled=True on an existing row and
#       refuses to touch a row whose model key isn't the deployment/<uuid> form. Plan C C4/C4b: local connectors meter usage with that key (they know
#       local_deployment_id via their connection link), so the quota engine's existing lookup charges the right deployment. Test: two orgs, same served name → independent rates.
#   AM27 (security review) Plan C read side: TokenManager/QuotaEngine rate lookup for provider in {vllm,ollama,llamacpp} uses model=rate_model_key(deployment)
#       resolved from the request's local_deployment_id; a lookup MISS for a local provider FAILS CLOSED (request refused with a clear 503-style error + counter
#       waddleai_local_rate_missing_total), never the default rate. End-to-end test: apply_pricing → route a request to that deployment → TokenManager charges exactly the virtual price.
#       No served-name local rate rows were ever shipped, so no cleanup migration.
#   AM14 llama.cpp deployments are created through the GPU Fleet wizard's "Engine" choice (Plan F F7), so the shared ChargebackStep covers llama.cpp pricing in the UI;
#       the legacy API-only llama.cpp create path (C2b) keeps accepting the price fields. Ollama pricing UI lives in pages/OllamaDeployments.jsx (D4b).
```
