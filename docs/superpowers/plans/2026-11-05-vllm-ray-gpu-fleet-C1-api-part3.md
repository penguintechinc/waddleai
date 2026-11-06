# vLLM on Ray GPU Fleet — Plan C, part 1c: per-deployment tokens and key rotation (Task C2c; continuation of `...-C1-api.md`)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Context:** Tasks C1, C2 and C2b are in `2026-11-05-vllm-ray-gpu-fleet-C1-api.md`; C3 is in `...-C1-api-part2.md`. This file holds Task C2c, which runs after C2b and before C4. Global Constraints and Review Focus in `...-C1-api.md` apply unchanged. Route prefix `/api/v1/fleet/`; every route requires scope `fleet:admin`.

**Spec:** `docs/superpowers/specs/2026-11-05-vllm-ray-gpu-fleet-design.md` §6.3 items 2 and 5. Amendments AM11, AM13, AM15 in `...-01-amendments.md`.

### Task C2c: Per-deployment tokens, cluster secret list and rotation

**Files:**
- Create: `services/management/app/services/fleet_secrets.py` (token minting, fingerprint, Secret read/write, external-source check)
- Modify: `services/management/app/api/v1/fleet_vllm.py` (mint on create, `rotate-token` route), `services/management/app/api/v1/llamacpp.py` (same for llama.cpp create path from C2b; `rotate-token`), `services/management/app/api/v1/fleet.py` (`GET /secrets`, `POST /secrets/<name>/rotate`), `tests/unit/management/conftest.py` (fixtures `fake_k8s`, `running_engine`)
- Test: `tests/unit/management/test_fleet_secrets_api.py`, `tests/unit/management/test_fleet_token_rotation.py` (engine bearer tests use the `running_engine` fixture)

**Interfaces:**
- Consumes: `connection_links.api_key` (encrypted column, existing); B5 middleware key path `/secrets/deployment-keys/<slug>` (Secret `waddleai-vllm-deployment-keys`, A1 AM16); B7 `VllmRayBackend.deploy`; A2b Secrets `waddleai-ray-auth` and `waddleai-ray-tls`, and the annotation `waddleai.penguintech.io/restart-generation`; AM15 env and `existingSecret` precedence.
- Produces: `fleet_secrets.mint_token() -> str` (`secrets.token_urlsafe(32)`, 256-bit); `fingerprint(value: str) -> str` (`sha256(value).hexdigest()[:12]`); `secret_source(name) -> "generated" | "external"`; routes below.

**Key precedence (AM15), applied to every rotatable key:** env var > `fleet.security.<key>.existingSecret` > chart-generated. A key with source `external` is never written by WaddleAI.

| Key | External source (AM15) | Chart-generated (A2b) |
|---|---|---|
| Ray token | env `WADDLEAI_RAY_AUTH_TOKEN` or `fleet.security.rayAuthToken.existingSecret` | Secret `waddleai-ray-auth` |
| Ray TLS | env `WADDLEAI_RAY_TLS_*_PATH` or `fleet.security.rayTls.existingSecret` | Secret `waddleai-ray-tls` |
| Per-deployment API key | none (WaddleAI always mints) | Secret `waddleai-vllm-deployment-keys`, key `<slug>` |

Token encryption at rest for `connection_links.api_key`: env `WADDLEAI_FLEET_TOKEN_ENCRYPTION_KEY` > `fleet.security.tokenEncryptionKey.existingSecret` > chart-generated `waddleai-fleet-token-key` (A2b); the existing Fernet key used by `fleet.py` credentials is the last fallback. Encryption key rotation is out of scope here (open item).

- [ ] **Step 1: Write the failing tests** (key assertion after `#`; sentinel `SENTINEL-TOKEN-VALUE-do-not-leak`)

```
# tests/unit/management/test_fleet_secrets_api.py
test_create_mints_token_and_stores_it_encrypted        # decrypt(connection_links.api_key) == Secret value at slug key; stored column != that value
test_create_response_never_contains_token              # token absent from body; api_key_fingerprint == sha256(token)[:12]
test_rotate_token_patches_only_slug_key                # 200; patch data keys == {slug}; new value != old; fingerprint changed
test_rotate_token_makes_no_serve_put                   # fake_serve.put_count == 0
test_rotate_token_secret_patch_failure_keeps_old_link  # patch raises -> 502 {"reason": "secret write failed"}; link api_key unchanged
test_delete_calls_remove_key_for_slug                  # DELETE order: backend.delete, then remove_key(slug) (last patch == {"data": {slug: None}}), then unlink, then row delete
test_rotate_token_writes_audit_event_without_value     # audit fleet.deployment.token_rotated present; token absent from it
test_llamacpp_rotate_token_same_contract               # 200; no "token" key in body
test_secrets_list_returns_source_fingerprint_rotated_at_never_value  # names == {ray-auth-token, ray-internal-tls}; no "value" key
test_secrets_list_requires_fleet_admin                 # fleet:read token -> 403
test_secrets_rotate_external_returns_409               # env var set -> 409 {"reason": "managed externally"}; no write
test_secrets_rotate_generated_bumps_restart_generation # 200; RayCluster pod-template restart-generation == previous + 1
test_secrets_rotate_unknown_name_404                   # "nope" -> 404
test_value_never_in_any_response_or_log                # sentinel absent from 3 response bodies and caplog
```

```
# tests/unit/management/test_fleet_token_rotation.py  (running_engine = fake vLLM and llama-server; AM11)
test_unauthenticated_request_rejected[vllm|llamacpp]               # no bearer -> 401
test_wrong_bearer_rejected_right_bearer_accepted[vllm|llamacpp]    # "Bearer wrong" -> 401; running_engine.token -> 200
test_rotated_token_invalidates_old_bearer                          # after rotate(): old bearer -> 401
```

- [ ] **Step 2: Run to verify they fail.** `pytest tests/unit/management/test_fleet_secrets_api.py tests/unit/management/test_fleet_token_rotation.py -v`. Expected: 19 failed or errored, 0 passed (routes absent). A missing fake is an error, never a skip.

- [ ] **Step 3: Implement**
  - `services/management/app/services/fleet_secrets.py` (create): `mint_token()` (`secrets.token_urlsafe(32)`); `fingerprint(value)` (`sha256(value).hexdigest()[:12]`); `secret_source(name)` (`external` when the AM15 env var is set or the `existingSecret` value is non-empty, else `generated`); `read_secret_value(name)` / `write_secret_value(name, value)` via the Kubernetes API (value never returned, logged, or put in an exception message); `remove_key(slug)` (merge-patch `data.<slug>` to `null`); `rotated_at` stored in the audit table, not in the Secret.
  - `services/management/app/api/v1/fleet_vllm.py` (modify): create path mints a token, merge-patches Secret `waddleai-vllm-deployment-keys` with `data.<slug>` only (chart-created, A1), encrypts the token into `connection_links.api_key`, then deploys; response adds `api_key_fingerprint`, never the token. `POST .../deployments/<uuid>/rotate-token`: (1) mint, (2) merge-patch `data.<slug>` only (no Serve redeploy; B5 middleware re-reads the file), (3) update `connection_links.api_key`, (4) audit `fleet.deployment.token_rotated` `{deployment, fingerprint_old, fingerprint_new}`. If (2) fails → 502 `{"reason": "secret write failed"}` and the old link value is kept (fail closed, never half-rotated). The old token stays valid up to 2 min (kubelet sync + grace); state this in the route docs.
  - `services/management/app/api/v1/llamacpp.py` (modify): same create path and `POST /api/v1/fleet/llamacpp/deployments/<uuid>/rotate-token`, with one extra step between (2) and (3): bump `waddleai.penguintech.io/restart-generation` on the llama.cpp head StatefulSet, so the head pod re-reads `--api-key-file` (AM18; workers untouched).
  - `services/management/app/api/v1/fleet.py` (modify): `GET /api/v1/fleet/secrets` returns items `{name, source, fingerprint, rotated_at}` for `ray-auth-token` and `ray-internal-tls` only (`fleet-token-encryption-key` is NOT listed: rotating it needs re-encryption of every `api_key` row, an open item). `POST /api/v1/fleet/secrets/<name>/rotate`: unknown name → 404; `source == external` → 409 `{"reason": "managed externally"}` before any write; `ray-auth-token`: generate 48 alnum (`secrets.choice`, Helm `randAlphaNum` alphabet), write Secret; `ray-internal-tls`: delete Secret `waddleai-ray-tls` so cert-manager reissues it (verified in E1). Both: bump `waddleai.penguintech.io/restart-generation` on the RayCluster pod templates (KubeRay rolls head and workers) and audit `fleet.secret.rotated` `{name, fingerprint_old, fingerprint_new}`; response `{name, source, fingerprint, rotated_at}`.
  - `tests/unit/management/conftest.py` (modify): fixture `fake_k8s` (in-memory Secret store with `secret_value`, `last_patch`, `last_token`, `force_value`, `fail_next_patch`) and fixture `running_engine` (starts the fake vLLM and llama-server from `tests/conformance/`; yields `url`, `token`; `rotate(engine)` helper).
  - DELETE (C1-api route) sequence: `backend.delete(dep)` → `fleet_secrets.remove_key(slug)` (merge-patch `data.<slug>` to `null`, AM16) → `unlink_deployment(db, dep)` (C3) → row delete.
  - All routes: `@validate_response` DTOs (no raw model), scope `fleet:admin`, tenant from the validated token.
  - Run: `pytest tests/unit/management/test_fleet_secrets_api.py tests/unit/management/test_fleet_token_rotation.py -v --cov=services/management/app/services/fleet_secrets --cov-branch`. Expected: 19 passed, 0 failed, 0 skipped; coverage ≥ 90% on `fleet_secrets.py`.

- [ ] **Step 4: Commit** (explicit files, no `git add -A`, never `git stash`)

```bash
git add services/management/app/services/fleet_secrets.py services/management/app/api/v1/fleet_vllm.py services/management/app/api/v1/llamacpp.py services/management/app/api/v1/fleet.py tests/unit/management/conftest.py tests/unit/management/test_fleet_secrets_api.py tests/unit/management/test_fleet_token_rotation.py
git commit -m "feat(fleet): per-deployment bearer tokens and cluster secret rotation API

<the executing session's attribution trailer>"
```

---
