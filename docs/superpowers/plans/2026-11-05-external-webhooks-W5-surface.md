# External Webhooks — W5 Surface (API, WebUI, CLI, Docs) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give operators the management surface: endpoint CRUD with license gating, `POST /webhooks/{id}/test`, deliveries list, replay, per-org JWKS, signing-key rotation, OpenAPI, a WebUI page, CLI `waddleai webhooks …`, docs and screenshots.

**Architecture:** Python management owns CRUD, writes `webhook_endpoints` and (for decision endpoints) the mirrored `plugins` row (`kind=external_http`) in **one transaction**, publishes the registry invalidation, and emits `webhook_endpoint.*` events. Test/replay call `svc-webhooks` over gRPC/HTTP admin. WebUI is React 18 + Vite + TS/JSX in `services/webui`; CLI is the Rust `clients/waddleai-cli`.

**Tech Stack:** Quart + `quart-schema` (`validate_request/response`), SQLAlchemy, OpenAPI 3.x (`openapi/v1.yaml`), React 18 + TanStack Query + Tailwind v4, Rust `clap`/`reqwest` (CLI), Playwright (screenshots), docs-site.

**Spec:** `docs/superpowers/specs/2026-11-05-external-webhooks-design.md` (all; esp. Flags & licensing, Security, Delivery semantics). Depends on W0; UI/CLI can land incrementally after W1.

## Global Constraints

- Authz: `require_scope` (`api/v1/auth.py`) with **new scopes `webhooks:read` / `webhooks:write`**; tenant middleware first; org-scoped; endpoints only ever visible/editable within the caller's org.
- Tier gate (two-layer, flag + `penguin_licensing.LicenseClient`, fail-closed on I/O error): **Free ⇒ refused with upgrade prompt**; **Professional** = events + decisions + content opt-in + `sig_alg` ∈ {1 Ed25519 default, 2 ECDSA-P256}; **Enterprise** required when `sig_alg ∈ {3 RSA-PSS-SHA256, 4 HMAC-SHA256}` **or** `auth_mode ∈ {2 mTLS, 3 SPIFFE}` **or** org `decision_composition = 3`. Check runs whenever `sig_alg`/`auth_mode` is not ECC. Refusal = `403` + upgrade prompt body.
- Labels: RSA/HMAC are **"legacy compatibility (Enterprise)"** in UI/docs/CLI help; docs recommend ECC; never presented as premium features.
- HMAC secret **shown once** at creation, stored Fernet-encrypted (`encrypt_credential`); never returned again, never logged.
- Every response through `@validate_response` DTOs with an exact-field-set regression test (no raw model objects); no PII; ids in API = `public_id` UUID.
- SSRF validation at registration (HTTPS only, no userinfo, reject private literal/ranges) plus runtime guard in `svc-webhooks`; private targets only via org `webhook_egress_cidr_allow` set by an admin.
- Streaming note: binding a `post_dispatch` decision endpoint turns that route non-streaming — surfaced in the UI as a warning and documented.
- Flags default OFF: `waddleai.webhooks-events`, `waddleai.webhooks-decisions`; kill-switch `waddleai.disable-external-decisions` exposed as ops runbook step.
- Keep docs current (`docs/*.md` ship with the change); OpenAPI via `make generate-openapi` + `make openapi-lint`; coverage ≥ 90%; screenshots in `docs/screenshots/` freshly captured and reviewed (`capturing-marketing-screenshots` skill).
- No hardcoded secrets; tokens only from env/files.

## Review Focus

1. **Pro user submits RSA/HMAC/mTLS/composition 3 via direct API** (not just the UI) → 403 (Task 2).
2. **Endpoint URL to metadata IP / `http://` / userinfo** rejected at CRUD (Task 2).
3. **Org A reads/edits/replays Org B's endpoint or delivery by guessing an id** → 404 (Task 3).
4. **Replay of a delivery whose endpoint was disabled/deleted or has changed `content_opt_in`** → re-checked at replay time, never leaks content (Task 4).
5. **Secret echo** — HMAC secret or private key in any later GET, list, log or event payload (Tasks 2, 5).

---

### Task 1: Scopes + DTOs + blueprint skeleton

**Files:** Create `services/management/app/api/v1/webhook_endpoints.py`; Modify `services/management/app/api/v1/__init__.py` (register), `shared/` scope definitions (`webhooks:read|write` in the auth service scope bundles: admin gets both, maintainer read-only per `security.md` bundles); Test `tests/unit/management/test_webhook_endpoints_authz.py`

**Interfaces:** `@dataclass(slots=True)` DTOs `WebhookEndpointIn`, `WebhookEndpointOut` (fields: `public_id, name, url, mode, event_types, content_opt_in, can_modify, sig_alg, auth_mode, timeout_ms, fail_mode, enabled, created_at, updated_at` — **no** secret fields), `DeliveryOut(public_id?, event_id, endpoint_id, attempt, status, http_status, latency_ms, next_attempt_at, last_error)`.

- [ ] **Step 1: Write tests**: `missing_scope_403`; `missing_tenant_claim_401`; `viewer_can_list_not_write`; `exact_field_set_of_WebhookEndpointOut`.
- [ ] **Step 2: Run** `pytest tests/unit/management/test_webhook_endpoints_authz.py -v` → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(mgmt): webhook endpoint blueprint + scopes`.

### Task 2: Endpoint CRUD with tier gate, SSRF validation, plugin mirroring

**Files:** Modify `webhook_endpoints.py`; Create `shared/webhooks/gate.py` (`check_webhook_tier(org_id, sig_alg, auth_mode, composition) -> None | raises TierDenied`) and `shared/webhooks/urlcheck.py` (`validate_endpoint_url(url, cidr_allow) -> None`); Test `tests/unit/management/test_webhook_endpoints_api.py`

**Interfaces:** `POST/GET/PATCH/DELETE /api/v1/webhooks/endpoints[/<uuid>]`; `PUT /api/v1/orgs/<uuid>/webhooks/settings {decision_composition, webhook_egress_cidr_allow}` (`webhooks:write`; composition 3 ⇒ Enterprise). Create of `mode=2` inserts the `webhook_endpoints` row **and** `plugins(kind=2, hook, position, scope_org_id, webhook_endpoint_id)` in one transaction, bumps `plugin_registry_version`, publishes the invalidation; emits `webhook_endpoint.created|updated|deleted` (W1 `emit_event`). HMAC secret returned once in the 201 body as `hmac_secret` (only when `sig_alg=4`).

- [ ] **Step 1: Write tests**: `free_tier_403_with_upgrade_prompt`; `pro_ed25519_201`; `pro_ecdsa_201`; `pro_rsa_403`; `pro_hmac_403`; `pro_mtls_403`; `pro_spiffe_403`; `pro_composition_3_403`; `enterprise_rsa_hmac_mtls_composition3_201`; `flag_off_refuses_with_404_or_403_documented_status`; `http_url_422`; `userinfo_url_422`; `metadata_ip_422`; `private_ip_allowed_only_with_cidr_allowlist`; `decision_endpoint_creates_plugin_row_same_txn` (and rollback leaves neither); `delete_removes_plugin_row_and_publishes_invalidation`; `hmac_secret_returned_once_and_stored_enc_prefixed`; `patch_sig_alg_to_rsa_rechecks_tier`; `cross_org_get_404`; `event_types_glob_validated`; `timeout_ms_bounds_1_to_3000_decision_500_proxy_cap_documented`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** Gate reuses `_flag_enabled`/`_entitled` pattern (`model_access_policies.py`). **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(mgmt): webhook endpoint CRUD with tier gate`.

### Task 3: Deliveries list + JWKS + key rotation

**Files:** Modify `webhook_endpoints.py`; Test `tests/unit/management/test_webhook_deliveries_api.py`

**Interfaces:** `GET /api/v1/webhooks/endpoints/<uuid>/deliveries?status=&event_type=&limit=&cursor=` (filters + pagination); `GET /api/v1/webhooks/jwks` (per-org, `kid`-tagged, active + next + in-window retired; public keys only; cacheable `Cache-Control: max-age=300`); `POST /api/v1/webhooks/keys/rotate` (creates `next` key; overlap window; both signatures sent, both in JWKS).

- [ ] **Step 1: Write tests**: `list_filters_by_status_and_pagination_and_empty_result`; `invalid_status_filter_422`; `other_org_endpoint_deliveries_404`; `jwks_contains_only_public_material` (no `d`, `p`, `q`, `private_key_enc`); `jwks_is_per_org`; `rotate_adds_next_key_both_in_jwks`; `rotate_requires_write_scope`; `delivery_out_exact_field_set`; `last_error_has_no_body_echo`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(mgmt): deliveries list, JWKS, key rotation`.

### Task 4: Test-send + replay

**Files:** Modify `webhook_endpoints.py`; Create `shared/webhooks/admin_client.py`; Test `tests/unit/management/test_webhook_test_replay.py`

**Interfaces:** `POST /api/v1/webhooks/endpoints/<uuid>/test` → svc-webhooks sends a signed `io.waddleai.webhook_endpoint.test` event, returns `{status, http_status, latency_ms}` (no body echo); `POST /api/v1/webhooks/deliveries/<id>/replay` (dead/failed only) re-enqueues with the **original `webhook-id`**, re-evaluating endpoint enabled/`content_opt_in`/tier at replay time.

- [ ] **Step 1: Write tests**: `test_send_reports_status_and_never_echoes_body`; `test_send_to_private_target_blocked`; `replay_dead_delivery_requeues_same_event_id`; `replay_ok_delivery_409`; `replay_after_endpoint_disabled_409`; `replay_after_opt_in_revoked_sends_without_content`; `replay_cross_org_404`; `replay_after_downgrade_to_free_403`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(mgmt): webhook test-send and replay`.

### Task 5: OpenAPI + contract + security checks

**Files:** Modify `openapi/v1.yaml` (via `make generate-openapi`); Test `tests/contract/` additions

- [ ] **Step 1: Write tests**: `openapi_contains_all_webhook_routes_with_scopes`; `no_secret_field_in_any_response_schema`.
- [ ] **Step 2: Run** `make openapi-lint && make test-contract` → FAIL. **Step 3: Implement** annotations; regenerate. **Step 4: Run** → PASS; `make lint`, `make test-security`.
- [ ] **Step 5: Commit** `docs(openapi): webhooks endpoints`.

### Task 6: WebUI page

**Files:** Create `services/webui/src/pages/Webhooks.jsx`, `Webhooks.css`, `services/webui/src/components/webhooks/{EndpointForm,DeliveryTable,KeyPanel}.jsx`; Modify `services/webui/src/App.jsx` (route + nav, flag-gated), `services/webui/src/hooks/useWebhooks.js` (TanStack Query); Test `services/webui/src/test/Webhooks.test.jsx`, `tests/e2e/webhooks.spec.js`

**Interfaces:** Page lists endpoints, create/edit form (mode, url, event-type globs, content opt-in, can-modify, timeout, fail mode, signing alg with ECC default and RSA/HMAC under a collapsed **"Legacy compatibility (Enterprise)"** group; auth modes mTLS/SPIFFE Enterprise-badged), deliveries table with replay, key rotation panel with JWKS URL, HMAC secret shown-once modal, streaming-downgrade warning when binding a decision endpoint at `post_dispatch`, upgrade prompt on 403.

- [ ] **Step 1: Write tests**: `ecc_is_default_and_legacy_group_collapsed`; `free_403_shows_upgrade_prompt`; `hmac_secret_modal_shown_once_not_in_dom_after_close`; `streaming_warning_shown_for_post_dispatch_decision`; `replay_button_only_for_dead_or_failed`; `no_secret_in_network_responses`; Playwright `create_event_endpoint_and_see_delivery`.
- [ ] **Step 2: Run** `npm test -- Webhooks` → FAIL. **Step 3: Implement** (use `penguin-react-dev`; Tailwind v4). **Step 4: Run** → PASS; Playwright `outputDir=/tmp/playwright-waddleai`, cleaned after run.
- [ ] **Step 5: Commit** `feat(webui): webhooks management page`.

### Task 7: CLI `waddleai webhooks …`

**Files:** Modify `clients/waddleai-cli/src/{cli.rs,api_client.rs,lib.rs}`; Create `clients/waddleai-cli/src/webhooks.rs`; Test `clients/waddleai-cli/tests/webhooks.rs` (wiremock)

**Interfaces:** subcommands `webhooks list|create|update|delete|test|deliveries|replay|rotate-key|jwks`; token from env/file only (never argv); HMAC secret printed once to stdout only with `--show-secret`; RSA/HMAC flags described as "legacy compatibility (Enterprise)".

- [ ] **Step 1: Write tests**: `create_sends_expected_json`; `hmac_secret_not_printed_without_flag`; `403_prints_upgrade_message_exit_nonzero`; `replay_rejects_non_uuid`; `token_never_appears_in_args_or_output`.
- [ ] **Step 2–4:** FAIL → implement → PASS (`cargo clippy -D warnings`, coverage ≥ 90%). **Step 5: Commit** `feat(cli): webhooks subcommands`.

### Task 8: Docs, docs-site, screenshots, runbook

**Files:** Create `docs/reference/webhooks.md` (events catalog, signing + `v1a` verification samples for Python/Node/Rust, JWKS, retries, composition modes, tier table, SSRF rules, streaming note, kill-switch runbook); Modify `docs/docs-site/` product section (via `penguin-docs-site-dev`), `docs/RELEASE_NOTES.md`, `README.md` feature list; screenshots under `docs/screenshots/`.

- [ ] **Step 1: Write test** `tests/docs/test_webhooks_docs.py::links_resolve_and_tier_table_matches_gate_constants` (docs tier table asserted against `shared/webhooks/gate.py` constants; every event type in the spec catalog present in docs).
- [ ] **Step 2: Run** → FAIL. **Step 3: Write docs**; capture full screenshot set (authenticated + unauthenticated, seeded mock data via `make seed-mock-data` adding 3–4 endpoints/deliveries) and visually review for error states. **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `docs: external webhooks reference, screenshots`.

### Task 9: Final gates + alpha E2E

- [ ] **Step 1:** `tests/e2e/test_webhooks_surface_e2e.py`: `create_endpoint_via_api_receives_signed_event_verified_via_jwks`; `kill_receiver_retries_then_dead_then_replay_succeeds`; `rotate_key_overlap_both_signatures_verify`.
- [ ] **Step 2:** On a freshly deleted alpha cluster run `make smoke-test test-e2e`; assert OTel counts printed: `webhook_deliveries_total{result}` ≥ 1, `webhook_delivery_latency` ≥ 1, `decision_latency` ≥ 1, outbox-lag gauge ≥ 1.
- [ ] **Step 3:** `make lint`, `make test-security`, `make test-contract` unchanged-green.
- [ ] **Step 4: Commit** `test(e2e): webhooks surface`.

## Self-Review

- **Spec coverage:** CRUD + test + deliveries + replay → T2–T4; tiering/labels → T2, T6, T7; JWKS/rotation → T3; OpenAPI → T5; WebUI/CLI → T6–T7; docs/screenshots → T8; verification list (alpha E2E, telemetry gate) → T9.
- **Assumption:** `decision_composition` and the egress CIDR allowlist are set via `PUT /orgs/<uuid>/webhooks/settings` (spec left the setter unspecified).
