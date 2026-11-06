# vLLM on Ray GPU Fleet — Amendments (authoritative)

Companion to `2026-11-05-vllm-ray-gpu-fleet-00-master.md`. Entries here override any conflicting sub-plan text. Executors read the master, this file, and their sub-plan.

```python
# Amendments 2026-11-05 (after Plan C discovery; authoritative over any conflicting sub-plan text):
#   AM1 Single migration 028 (Plan B, task B2) also carries: vllm_deployments.expose_via_proxy BOOLEAN NOT NULL DEFAULT true,
#       vllm_deployments.prefer_local JSON NULL, vllm_deployments.comparison_model VARCHAR(255) NOT NULL (commercial "provider/model"
#       used for price suggestion + savings), model_registry.public_id UUID UNIQUE NOT NULL (backfilled), model_registry.params_b NUMERIC(8,2) NULL,
#       connection_links.owner_org_id BIGINT NULL FK organizations (NULL = shared, as today). No migration 029.
#   AM2 Pricing: there is no per-org price store; pricing is the global `token_conversion_rates` table (provider, model, input_rate, output_rate per token).
#       On deployment create/price PATCH, C6 upserts a token_conversion_rates row (provider="vllm", model=served_model_name,
#       input_rate = base_cost_per_waddleai_token × 1e6 / price_input_per_1m, output_rate likewise, base_cost_per_waddleai_token = global default 0.001)
#       — rates are DIVISORS (LLM tokens per credit, shared/utils/token_manager.py:134) — so QuotaEngine's existing lookup charges the virtual price unchanged.
#       No WADDLEAI_VLLM_USD_PER_CREDIT / WADDLEAI_VLLM_PRICE_CLASS_MAP env vars. Price suggestion = comparison_model's rates × discount
#       (config key WADDLEAI_VLLM_PRICE_DISCOUNT_PCT, default 50). Savings = Σ tokens × comparison_model rates − Σ tokens × snapshot unit price.
#       token_usage is a per-key/per-day rollup: C6 opens a new row when (local_deployment_kind, local_deployment_id, unit_price_input, unit_price_output) differ.
#   AM3 Org isolation: offers/routing (C5) exclude any connection_links row whose owner_org_id is set and ≠ the request's org; vllm links are
#       created with owner_org_id = deployment org. Connector loading stays global (existing behaviour); selection is where isolation is enforced.
#   AM4 /vllm/models returns model_registry.public_id (never null) and params_b (null → UI asks the admin).
#   AM6 (owner 2026-11-05) Default price = amortized true cost, admin-overridable, spec §4.7. Plan B: enums.PriceMode(IntEnum): AUTO_TCO=1; MANUAL=2;
#       new module shared/fleet/vllm_ray/tco.py:
#         @dataclass(slots=True) class NodeClassCost: cpu_usd:float; ram_gb:int; nvme_gb:int; nvme_pro:bool; network_devices:int; node_base_watts:int; gpus_per_node:int; node_base_usd:float|None=None  # explicit override
#         @dataclass(slots=True) class GpuClassCost: gpu_class:str; card_usd:float; tdp_watts:int; est_tokens_per_s:float; node:NodeClassCost
#         @dataclass(slots=True) class TcoInputs: kwh_usd:float=0.18; lifecycle_years:int=10; utilization_pct:int=50; input_ratio:float=0.2; ram_usd_per_gb:float=1.0; nvme_usd_per_gb:float=1.0; nvme_pro_usd_per_gb:float=2.0; network_device_usd:float=400.0
#         @dataclass(slots=True) class TcoResult: hourly_cost_usd:float; gpu_hourly_usd:float; node_hourly_usd:float; node_base_usd:float; tokens_per_s_used:float; source:str  # "observed"|"estimate"
#                                                 price_input_per_1m:float; price_output_per_1m:float; breakdown:dict
#         def node_base_usd(node:NodeClassCost, inputs:TcoInputs) -> float   # explicit override wins, else cpu + ram×rate + nvme×(pro?2:1 rate) + devices×400
#         def compute_tco(layout:Layout, costs:dict[str,GpuClassCost], inputs:TcoInputs, params_b:float, observed_tokens_per_s:float|None) -> TcoResult
#         (formulas verbatim from spec §4.7; estimate = est_tokens_per_s × min(1, 12/params_b) when no observation; kwh_usd<=0 → ValueError("kwh_usd must be set"))
#       B2 migration 028 adds vllm_deployments.price_mode INT NOT NULL DEFAULT 1, price_breakdown JSON NULL, observed_tokens_per_s NUMERIC(10,2) NULL.
#       Plan A values: fleet.vllmRay.tco.{kwhUsd:0.18, lifecycleYears:10, utilizationPct:50, inputRatio:0.2, ramUsdPerGb:1, nvmeUsdPerGb:1, nvmeProUsdPerGb:2, networkDeviceUsd:400} and
#       workerGroups[].cost.{cardUsd,tdpWatts,estTokensPerS,cpuUsd,ramGb,nvmeGb,nvmePro,networkDevices,nodeBaseWatts,gpusPerNode,nodeBaseUsd?}; the backend config row carries the same keys (editable via PUT /fleet/backends/<id>).
#       Plan C: GET /vllm/price-suggestion?model_ref=&vendor=&gpu_class=&tp=&pp=&replicas= returns TcoResult (replaces the discount suggestion; WADDLEAI_VLLM_PRICE_DISCOUNT_PCT removed);
#       create body accepts price_mode (default 1): AUTO → server computes price_* via compute_tco and ignores client price_*; MANUAL → price_* required > 0;
#       PATCH accepts price_mode / price_* (manual override at any time); C3 reconcile loop recomputes AUTO prices daily from observed_tokens_per_s
#       (rolling 24h p50 output tok/s from usage), upserts token_conversion_rates, writes event "price recalculated". Plan D: wizard step 4 shows the
#       breakdown + "Set price manually"; backend settings panel edits tco inputs and per-class/node costs.
#   AM8 (owner 2026-11-05) Static GPU price reference: shared/fleet/vllm_ray/gpu_price_reference.py with
#       GPU_PRICE_REFERENCE: dict[str, GpuRef]  (@dataclass(slots=True, frozen=True) class GpuRef: vendor:int; gpu_class:str; vram_gb:int; tdp_watts:int; avg_street_usd:int; availability:str)
#       AS_OF = "2026-11-05"; generated from three JSON files ALREADY PLACED at docs/reference/gpu-price-reference-{nvidia,amd,intel}.json (157 rows: 85/53/19, all priced;
#       row fields: vendor, model, class (the slug, e.g. nvidia-rtx4090 — B3's normalize_class MUST emit these exact slugs), vram_gb, tdp_watts, avg_street_usd, range_usd, availability, confidence H/M/L, sources),
#       covering EVERY discrete GPU released since 2018 by the three vendors (consumer, workstation, datacenter) — owner requirement, because unknown classes hard-refuse;
#       GpuClassCost.card_usd and tdp_watts become Optional — when None, compute_tco resolves them from GPU_PRICE_REFERENCE[gpu_class];
#       unknown class with no explicit values → ValueError("no price reference for <class>; set cost.cardUsd and cost.tdpWatts"); breakdown records price_source: "reference"|"configured".
#       Values: workerGroups[].cost.cardUsd / tdpWatts optional. Backend settings panel shows the reference value greyed as the default. Plan B task "B4c" owns the module + a test that every class slug in the reference parses through normalize_class conventions and that the fallback + override + unknown paths work.
#   AM9 Config hand-off + key convention: the chart renders ConfigMap waddleai-vllm-ray-fleet-config (JSON, snake_case keys: tco.*, worker_groups[].cost.*)
#       and sets WADDLEAI_VLLM_RAY_BACKEND_CONFIG=<mounted path> on management; VllmRayBackend.__init__ loads it as DEFAULTS and merges the
#       fleet_backends.config row on top (row wins) — Plan B B7 owns the merge + test. Helm values stay camelCase; everything past the chart boundary
#       (ConfigMap JSON, API DTOs, UI payloads) is snake_case. D1-part2 tests must use snake_case.
#   AM10 (owner 2026-11-05) ONE pricing formula for ALL locally hosted models — Ollama and llama.cpp fleet deployments get the same AUTO_TCO/MANUAL
#       pricing, rate-row upsert, daily recompute, snapshot and chargeback as vLLM:
#       - shared/fleet/pricing.py (Plan B, new task B4d): class LocalDeploymentKind(IntEnum): VLLM=1; OLLAMA=2; LLAMACPP=3 (in enums.py);
#         def gpu_set_for(kind:int, row) -> tuple[list[tuple[str,int]], NodeClassCost|None]   # [(gpu_class, count)], node class — vLLM: from the deployment layout + inventory;
#         Ollama/llama.cpp: gpu_class from node label waddleai.penguintech.io/gpu-classes of the deployment's node_selector/node, count from its GPU resource request (gpu_config / gpu_count), VRAM/TDP/price from GPU_PRICE_REFERENCE
#         async def apply_pricing(db, kind:int, row, inputs:TcoInputs, costs:dict[str,GpuClassCost], observed_tokens_per_s:float|None) -> TcoResult   # computes (AUTO) or validates (MANUAL), writes price_* + price_breakdown on the row, upserts token_conversion_rates (provider = "vllm"|"ollama"|"llamacpp", model = served/model name)
#       - Migration 028 adds to ollama_deployments AND llamacpp_deployments: price_mode INT NOT NULL DEFAULT 1, price_input_per_1m, price_output_per_1m NUMERIC(12,6) NOT NULL DEFAULT 0,
#         price_breakdown JSON NULL, observed_tokens_per_s NUMERIC(10,2) NULL, comparison_model VARCHAR(255) NULL.
#       - token_usage: REPLACE fleet_deployment_id with local_deployment_kind INT NULL + local_deployment_id BIGINT NULL (no FK; kind-discriminated); unit_price_* unchanged. All plans use the new names.
#       - Plan C: price-suggestion becomes GET /api/v1/fleet/price-suggestion?kind=&... (vllm keeps its alias); ollama.py / llamacpp.py create+PATCH accept price_mode/price_*/comparison_model;
#         C3's daily recompute iterates all three kinds; Ollama/llama.cpp connectors set local_deployment_kind/id on usage rows (C4b); chargeback + savings endpoints move to /api/v1/fleet/chargeback and /api/v1/fleet/savings covering all kinds (vllm paths kept as aliases).
#       - Plan D: the Chargeback wizard step is a shared component (ChargebackStep) reused in the existing Ollama/llama.cpp deployment forms (find them in services/webui; if none exist, add price fields to their API-driven forms where the deployments are created); Overview savings card covers all local kinds; chargeback report filters by kind.
#   AM11 (owner 2026-11-05) Auth, spec §6.3: bearer/JWT auth MANDATORY on every inference endpoint (vLLM --api-key / Serve auth middleware, llama-server --api-key-file),
#       per-deployment token minted by WaddleAI (secrets.token_urlsafe(32)), stored encrypted in connection_links.api_key, rotated on redeploy + POST .../deployments/<uuid>/rotate-token;
#       Ray token auth REQUIRED (pin must support it); Ray internal TLS default on (SPIRE SVIDs else cert-manager); Cilium mutual auth default on;
#       links with no app-level auth (rpc-server, Ray GCS) need mutual auth OR internal TLS, else multi-node layouts are REFUSED (fail closed);
#       values fleet.security.mutualAuth (default true), fleet.vllmRay.rayInternalTls (default true), fleet.vllmRay.rayAuthToken secretRef;
#       Plan A: render tests for the auth blocks + CNP authentication.mode; Plan B/F: deploy refuses when neither link-auth mechanism is enabled (test);
#       Plan C: token minting/rotation + regression tests that unauthenticated requests to vLLM/llama-server get 401; E adds E12 (unauthenticated request rejected on dal2beta).
#   AM12 Verified during planning (docs/superpowers/specs/2026-11-05-vllm-ray-research-notes.md; version/issue numbers to be re-checked against upstream by the executor):
#       - Ollama has NO native multi-node (only forks) → Ollama stays single-node; UI copy: "Ollama runs single-node; use llama.cpp RPC or vLLM on Ray for multi-node". No fork vendored.
#       - llama.cpp rpc-server flags: -H host (default 127.0.0.1 → must bind 0.0.0.0 in-pod), -p port (50052), -d device, -m mem, -c cache; coordinator: llama-server --rpc h:p,h:p (env LLAMA_ARG_RPC); layer split default;
#         all binaries same llama.cpp commit (one image tag for head + workers); no auth/TLS → AM11 link-auth requirement applies; expect 20-50% decode penalty across nodes (shown in UI copy).
#       - Ray token auth: RAY_AUTH_MODE=token from 2.52.0, default-on from 2.61.0; protects GCS/Jobs/Dashboard/Serve REST, NOT Serve HTTP ingress → Plan A pins Ray ≥ 2.61.0 (replace the 2.55.0 candidate), token via RAY_AUTH_TOKEN_PATH from a Secret;
#         Serve HTTP ingress gets the API-key FastAPI middleware (Plan B serve_config adds it to the Serve app; Plan C mints the key).
#       - Ray TLS envs read once at startup (no reload) → SPIRE SVID rotation requires pod restart: use SPIRE TTL ≥ 24h for the fleet entries or cert-manager certs with 90-day TTL + a rolling restart hook on renewal (Plan A).
#       - ray.serve.llm has no native API key → middleware confirmed required. llama-server: --api-key-file, --ssl-cert-file/--ssl-key-file (image must be built with -DLLAMA_OPENSSL=ON).
#       - Cilium mutual auth (SPIFFE/SPIRE) is beta since 1.14; needs WireGuard/IPsec for encryption → values fleet.security.mutualAuth + fleet.security.wireguard (documented prerequisite).
#   AM13 (owner 2026-11-05) Keys: Helm generates every operator/admin key at install — Ray auth token (Secret waddleai-ray-auth, randAlphaNum 48, kept across upgrades via lookup),
#       internal-TLS CA + certs (cert-manager Issuer/Certificate per cluster), bootstrap secrets; never in values. Plan A owns generation + "unchanged on upgrade" render tests.
#       Plan C: GET /api/v1/fleet/secrets (name, fingerprint sha256[:12], rotated_at — never the value), POST /api/v1/fleet/secrets/<name>/rotate (management writes the new Secret,
#       triggers rolling restart of RayCluster head+workers via KubeRay restart annotation, writes audit event), POST /vllm/deployments/<uuid>/rotate-token and the llama.cpp equivalent;
#       scope fleet:admin; tests incl. "value never in response/logs". Plan D: Backend settings panel "Rotate" buttons + CLI `waddleai fleet secrets {list,rotate}`.
#   AM7 D1 sends model_ref as the registry name or HF repo id string (not public_id) — confirmed. B2's seed must use the real model_registry column names (verify against models_sqlalchemy.py ~587).
#   AM5 C7 prefer-local: resolve_prefer_local in the routing engine + destination pass-through to dispatch is in scope; executor must trace
#       routing-decision → dispatch end to end and add an integration test that a prefer-local rule actually selects the vllm destination.
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
#   AM14 llama.cpp deployments are created through the GPU Fleet wizard's "Engine" choice (Plan F F7), so the shared ChargebackStep covers llama.cpp pricing in the UI;
#       the legacy API-only llama.cpp create path (C2b) keeps accepting the price fields. Ollama pricing UI lives in pages/OllamaDeployments.jsx (D4b).
```
