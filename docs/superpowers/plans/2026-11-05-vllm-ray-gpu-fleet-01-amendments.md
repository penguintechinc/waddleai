# vLLM on Ray GPU Fleet — Amendments (authoritative)

Companion to `2026-11-05-vllm-ray-gpu-fleet-00-master.md`. Entries here override any conflicting sub-plan text. Executors read the master, this file, and their sub-plan.

Continued in 2026-11-05-vllm-ray-gpu-fleet-01b-amendments.md (AM14–AM27).

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
```
