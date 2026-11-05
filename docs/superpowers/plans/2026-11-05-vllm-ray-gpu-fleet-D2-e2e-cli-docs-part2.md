# vLLM on Ray GPU Fleet — Plan D, part 2b: docs (Task D8; continuation of `...-D2-e2e-cli-docs.md`)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Context:** Tasks D5b–D7 are in `...-D2-e2e-cli-docs.md`; D9 stays there. This file holds Task D8, moved to keep each file under 25,000 characters. Interface Decisions and Review Focus in `...-D2-e2e-cli-docs.md` apply unchanged.

### Task D8: Admin guide, README, docs-site, screenshots, OpenAPI link

**Files:**
- Create: `docs/operations/gpu-fleet-vllm-ray.md` (canonical admin guide)
- Create: `docs/docs-site/docs/operations/gpu-fleet-vllm-ray.md` (site twin; same content, kept identical by a `diff` check in step 4)
- Modify: `docs/docs-site/mkdocs.yml:55-56` (add nav entry `- GPU Fleet (vLLM on Ray): operations/gpu-fleet-vllm-ray.md` after the SLO line)
- Modify: `README.md:136` (add a feature bullet after the GPU inference fleet bullet: "vLLM on Ray: deploy open-weight models across mixed NVIDIA/AMD/Intel GPU nodes from the admin UI, behind a flag; see docs/operations/gpu-fleet-vllm-ray.md")
- Modify: `docs/RELEASE_NOTES.md` (prepend an entry under the next version; keep 25K-char file-size rule in mind)
- Create: `docs/screenshots/gpu-fleet-overview.png`, `gpu-fleet-deployments.png`, `gpu-fleet-wizard-fit.png`, `gpu-fleet-wizard-review.png`, `gpu-fleet-backend.png`
- Modify: `docs/operations/gpu-fleet-vllm-ray.md` links to the OpenAPI file `openapi/v1.yaml` (GPU fleet paths).

**Guide outline (exact headings):**
1. What this does (3 sentences, no AI jargon)
2. Prerequisites: node labels (`waddleai.penguintech.io/ai-node=true`, `/gpu-vendor`, `/gpu-classes`); device-plugin class map (`fleet.vllmRay.nvidiaClassMap`); Storage class for the model store; Flags `waddleai.fleet_v2` and `waddleai.fleet-vllm-ray`.
3. Deploy walkthrough (six steps, screenshots of steps 2 and 6)
4. What "fits" means (the 20% KV headroom, the 1.5 GB per GPU context, the plain-language sentence format)
5. Chargeback and savings (virtual price for every local kind: vLLM, Ollama, llama.cpp; the verbatim copy; quota behavior; the report filters by kind)
6. Troubleshooting table: one row per `DEPLOYMENT_STATUS_LABEL` (8 rows) and per `INVENTORY_STATE_LABEL` (4 rows); columns: Symptom · Meaning · What to do.
7. Command line (`waddleai fleet vllm`)
8. Scope limits (Intel single-GPU replicas, AMD/Intel homogeneous per node, no training, non-WaddleAI GPU pods unsupported on AI nodes)

- [ ] **Step 1: Failing check** — a doc-lint test `tests/unit/test_gpu_fleet_docs.py` asserts: the guide contains a row for every status label string in `vllmFleetLabels.js` (parse the JS file with a regex; count must be 12 = 8 + 4; zero matched = FAIL); the 5 screenshot files exist; the twin files are byte-identical; the mkdocs nav contains the entry.

Run: `.venv/bin/pytest tests/unit/test_gpu_fleet_docs.py -v`
Expected: FAIL (guide missing). The same test also asserts the `Authentication & keys` heading and every env name in that section.

- [ ] **Step 2: Write the guide** (terse, tables over bullets, per the house style).

Checkable result: `docs/operations/gpu-fleet-vllm-ray.md` exists, and `grep -c '^## ' docs/operations/gpu-fleet-vllm-ray.md` returns 9 with these H2 headings in order: `## What this does`, `## Prerequisites`, `## Authentication & keys`, `## Deploy walkthrough`, `## What "fits" means`, `## Chargeback and savings`, `## Troubleshooting`, `## Command line`, `## Scope limits`. The docs-site twin is then copied byte-for-byte (`cp docs/operations/gpu-fleet-vllm-ray.md docs/docs-site/docs/operations/gpu-fleet-vllm-ray.md`).

**Authentication & keys section (AM11, AM13, AM15), placed after item 2 of the outline:**
- Every inference endpoint requires a per-deployment bearer token; the proxy is the only holder. `Rotate token` rotates it and restarts the deployment.
- Cluster keys: list with `waddleai fleet secrets list`; rotate with `waddleai fleet secrets rotate <name>` (restarts Ray head and workers). Values are never shown; only fingerprints and rotation times.
- `managed externally`: a key supplied by env var or `existingSecret` cannot be rotated from WaddleAI (409). Rotate it in your secret store.
- Table (name · env var · values key · precedence):

| Key | Env var | `existingSecret` key | Precedence |
|---|---|---|---|
| Ray auth token | `WADDLEAI_RAY_AUTH_TOKEN` | `fleet.security.rayAuthToken.existingSecret` (key `token`) | env > existingSecret > chart-generated |
| Ray TLS | `WADDLEAI_RAY_TLS_CERT_PATH`, `WADDLEAI_RAY_TLS_KEY_PATH`, `WADDLEAI_RAY_TLS_CA_PATH` | `fleet.security.rayTls.existingSecret` (keys `tls.crt`, `tls.key`, `ca.crt`) | env > existingSecret > chart-generated |
| Connection-link encryption | `WADDLEAI_FLEET_TOKEN_ENCRYPTION_KEY` | `fleet.security.tokenEncryptionKey.existingSecret` (key `fernet`) | env > existingSecret > chart-generated |
| SPIFFE trust domain | `WADDLEAI_SPIFFE_TRUST_DOMAIN` (not a secret) | n/a | env > `fleet.security.spiffeTrustDomain` (default `penguintech.io`) |

- Infisical example: sync a secret to a Kubernetes Secret with the Infisical Kubernetes operator, then set `fleet.security.rayAuthToken.existingSecret: ray-token-from-infisical`.
- External Secrets Operator example: `ExternalSecret` with `target.name: ray-token-from-vault`, `data: [{secretKey: token, remoteRef: {key: waddleai/ray, property: token}}]`, then set `fleet.security.rayAuthToken.existingSecret: ray-token-from-vault`.


- [ ] **Step 3: Capture screenshots** with the `capturing-marketing-screenshots` skill (mock data seeded via D6). Checkable result: `ls docs/screenshots/gpu-fleet-*.png` lists exactly `gpu-fleet-backend.png`, `gpu-fleet-deployments.png`, `gpu-fleet-overview.png`, `gpu-fleet-wizard-fit.png`, `gpu-fleet-wizard-review.png`. Each file is opened with the Read tool and shows no error banner; `gpu-fleet-wizard-fit.png` shows a real fit sentence (`Fits on 1 node ...`), not a refusal. Reference only; do not re-implement the procedure here.

- [ ] **Step 4: Run** `.venv/bin/pytest tests/unit/test_gpu_fleet_docs.py -v` → PASS (12 rows matched, 5 screenshots present, twins identical). Then the markdown gate: there is no markdown-lint Makefile target (`grep -n lint Makefile` shows none for markdown), so run `.venv/bin/pip install -r docs/docs-site/requirements.txt` (pinned; mkdocs is not in `.venv` today), then `.venv/bin/mkdocs build --strict -f docs/docs-site/mkdocs.yml` → exit 0, and `make openapi-lint` → exit 0 (spectral, `Makefile:107`).

- [ ] **Step 5: Commit**

```bash
git add docs README.md
git commit -m "docs: vLLM on Ray admin guide, screenshots, nav and README feature"  # + the executing session's attribution trailer
```

---
