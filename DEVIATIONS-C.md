# DEVIATIONS (Plan C, merged API + proxy/chargeback streams)

## Integration
- Migration 029 was copied verbatim by the proxy stream; one kept. Alembic head is 030 (`local_generation_samples`); head pins in test_migration_019/026/029 point at 030. Roundtrip verified on pgvector:pg17 (digest-pinned): upgrade head -> downgrade 028 -> upgrade head.
- test_scope_authz route count 146 = 142 (API: +14 vllm fleet, +1 llamacpp rotate) + 4 (chargeback reports). ROUTE_MODULES/blueprints are the union; openapi/v1.yaml regenerated.
- Reconciler wiring: record_inventory / record_deployment_counts (per tick, all deployments) / record_placement (create route admission outcomes); daily AUTO recompute uses observed_tokens_per_s(24h) with the per-replica GPU-class estimate as sanity bound (estimate stays the fallback on None); prune_generation_samples(72h) once per day; default prefer-local factory is the real create_prefer_local_rule (sync, run via to_thread; warning stub removed). `RollupUsageSource` replaced by `SampleUsageSource`.
- Create with an invalid prefer_local -> 400 (validate_prefer_local) before anything is written. apply_pricing runs on create and price PATCH for vllm, ollama, llamacpp. LOCAL_PROVIDERS comes only from shared/routing/providers.py.

## API stream
- Pricing via shared/fleet/vllm_pricing.price_deployment (params_b: body > name > registry); scale-to-zero prices at 1 replica. comparison_rates returns a dict.
- Weights: canonical at <store>/<org_id>/<model_ref>, slug is a symlink (management mounts the model-store PVC, chart A concern); pull Job = shared/fleet/vllm_ray/model_pull.py; progress from a .progress file.
- Create ALWAYS mints a DISABLED connection_links row (canary/rotation need it); reconciler enables per expose_via_proxy.
- Assumed k8s names/envs: WADDLEAI_FLEET_NAMESPACE, WADDLEAI_RAY_CLUSTER_NAME, WADDLEAI_RAY_AUTH_SECRET_KEY, external-source *_EXISTING_SECRET flags; WADDLEAI_FLEET_TOKEN_ENCRYPTION_KEY aliases CREDENTIAL_ENCRYPTION_KEY.
- Operator identity = system:config scope + org == WADDLEAI_SYSTEM_ORG_ID (unset fails closed); rotation audit row doubles as cooldown store.
- Migration 029: model_configs.owner_org_id + deployment_public_id; UNIQUE(model_name) replaced by partial unique indexes (global / per-org). Any model_name lookup must filter owner_org_id.
- llama.cpp key mint/rotate gated on both fleet flags; Ollama PUT prices against pre-update gpu_config.
- Tests use a real sqlite penguin-dal DB (vllm_harness). Pre-existing, not fixed: PageRequest.limitby (start,stop) vs penguin_dal (offset,limit) for page>=2 in older routes.

## Proxy / chargeback stream
- Engine-reported token counts validated (validate_token_count); invalid -> estimated + flagged; vllm/ollama/llamacpp streams now return real counts (previously metered zero).
- Fleet-managed Ollama/llama.cpp links fail closed without a key. FOLLOW-UP: Ollama itself does not validate Authorization; needs an auth layer in front of fleet Ollama pods.
- Rates: local usage rate = provider kind + model deployment/<public_id>; miss -> LocalRateMissingError + 503 local_rate_missing pre-dispatch. process_usage clamps negative counts to 0.
- RoutingStage now loads the org's enabled prefer-local rules only; all model_configs readers use visible_model_configs (own org + shared; owner wins ties).
- Throughput: per-request samples table local_generation_samples (migration 030; timing_source engine/proxy, user_id). Pricing uses the sustained AGGREGATE rate, not a per-request rate (batching would overstate cost per token): `observed_aggregate_tokens_per_s` buckets samples into 5-min buckets by completion time, rate = sum(tokens)/300 s, answer = p90 of busy buckets, treated as the deployment TOTAL. Engine-timed preferred; per-principal tokens capped at 20% of a bucket (a bucket with <5 principals counts for nothing); needs >=5 principals and >=6 busy buckets else None; absurd per-request samples dropped and counted. Aggregated in SQL (GROUP BY bucket,user,source; no row LIMIT to flood). Demand is not capacity: pricing throughput = max(estimate, observed) -- an observed aggregate <= the deployment-total estimate is ignored (observed can only LOWER the price); > 10x the estimate is rejected (counted).
- Chargeback routes in fleet_chargeback.py (fleet:admin, behind waddleai.fleet_v2); money as decimal strings.

## Review-fix round (pricing sink, rotation atomicity, tenancy)

- `compute_tco` now clamps throughput to `max(estimate_total, observed)`; observed may only lower the price. A zero/negative observed value is ignored rather than rejected. `apply_pricing`/`price_deployment` record `observed_tokens_per_s` only when it set the price (else NULL).
- `observed_aggregate_tokens_per_s` returns None when the estimate is missing/<=0 (was: accepted unbounded). `estimate_aggregate_tokens_per_s` catches only ValueError/TypeError/KeyError/AttributeError/ArithmeticError, logs with traceback and counts `estimate_unavailable`.
- Another org's backend/deployment is now 404 (was 403) on every fleet route: no existence oracle.
- vLLM rotate-token: 409 when `connection_link_id` is missing (nothing written); link-update failure restores the previous Secret value and returns 502 `link update failed; previous token restored`; success writes an `audit_log` row `fleet.deployment.token_rotated/<slug>`.
- llama.cpp rotate-token: link updated immediately after the Secret write; restart failure is 502 `rotated, restart pending, retry`.
- `SecretWriteFailed.partially_applied`; cluster-secret rotate returns 502 `rotated, restart pending, retry` and releases the cooldown claim so retry works. `ray-internal-tls` waits (bounded, 60s) for the reissued Secret before bumping the restart.
- Reported 0 tokens beside non-empty prompt/output text is estimated and flagged (usage-missing counter).
- `waddleai-hf-<id>` Secret deleted on pull-start failure and on deployment delete.
- Create prices after the name pre-check; a lost insert race discards the rate row.
- Reconciler: failed local throughput lookup counts `reconcile_errors` and keeps the last price; counts gauge and local-kind scans gated by `deployment_interval_s`; running-AUTO rows filtered by owned backend ids in SQL (ownerless rows still returned to warn).
