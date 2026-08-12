# App-Specific Context

> ✅ **This file IS safe to modify.** Add your app-specific rules, context, and requirements here.

## About This App

WaddleAI is a self-hosted AI gateway and management platform: a token-efficiency,
local-knowledge and security layer in front of both local and commercial models.

- **Data plane** — `proxy/`, Quart on hypercorn (`apps.proxy_server.main:app`, :8080).
  OpenAI-compatible (`/v1/chat/completions`) and Anthropic-compatible (`/v1/messages`)
  endpoints, through an ordered `ProxyPipeline`: auth → rate limit → security →
  memory → dispatch → metering.
- **Control plane** — `services/management/`, Quart on hypercorn (`asgi:app`, :8001).
- **Web UI** — `services/webui/`, React 18 + Vite, built on Node 24, served by nginx.
- **Deployment** — Kubernetes via Helm only (`k8s/helm/waddleai`). Docker Compose is
  deprecated; there is no root `docker-compose.yml`.

## App-Specific Rules

### ⚠️ Two deliberate exceptions to the global standards — do not "fix" them

Both are approved, narrow, and carry their own guardrails. Full detail and the exact
constraints live in **`.claude/security.local.md`**; read it before touching model
registration, fleet placement, or the model registry.

1. **Two PRC-origin generative-media models are permitted** — `Kolors` and
   `Open-Sora`, under spec §2.2a. Global-admin opt-in, off by default, **generation
   roles only** (never `security-audit` / `routing-classifier` / `embeddings` /
   `summarize`). Every other PRC-origin model stays denied, CogVideoX included.
2. **Non-commercial weights are permitted in the Free tier only** — `MusicGen`,
   `AudioLDM 2`, under spec §2.3's third licence class. Hard-disabled in
   Professional and Enterprise; the gate is a **deny**, not a missing unlock.

### Model defaults

Resolve through the model registry, not hardcoded strings.

| Role | Default | Notes |
|---|---|---|
| Routing classifier | `gemma4:e2b` | **No `2b` tag exists** — Gemma 4 publishes `e2b`/`e4b`/`12b`/`26b`/`31b`. Gemma 4 is Apache-2.0, unlike Gemma 1–3 |
| Security auditor (text) | `shieldgemma:2b` | **ShieldGemma 1**, deliberately. ShieldGemma 2 is a 4B **image-only** classifier and cannot classify text — swapping it in disables text filtering rather than upgrading it |
| Embeddings (Ollama path) | `nomic-embed-text` | 768-dim |
| Embeddings (in-process path) | `all-MiniLM-L6-v2` | 384-dim, `SentenceTransformer`. **Not** interchangeable with the above — `rag_integration.py` hardcodes `vector_size = 384` |

### Framework and library constraints

- **Quart + hypercorn only.** No Flask, no FastAPI, no gunicorn.
  `services/management/wsgi.py` is vestigial Flask-era dead code — the Dockerfile
  runs `asgi.py`. Do not treat `wsgi.py` as an entry point.
- **`penguin-dal` for all runtime database access.** SQLAlchemy + Alembic are
  schema/migration authority only. Raw `pydal` imports are not permitted.
- **Valkey, not Redis.** The `REDIS_URL` env var name is historical — the proxy reads
  `REDIS_URL`, management reads `CACHE_HOST`/`CACHE_PORT`. Setting only one will not
  move both.
- **`penguin-aaa`** for auth. Flask-Security-Too is deprecated.

## Key Files & Locations

| Path | What |
|---|---|
| `docs/superpowers/specs/2026-07-09-waddleai-platform-spec.md` | Authoritative platform spec — the source of truth for policy questions |
| `.claude/security.local.md` | The two standards exceptions above, with their guardrails |
| `proxy/apps/proxy_server/pipeline/stages.py` | Ordered `ProxyPipeline` stages |
| `shared/security/content_filter.py` | Tiered content filter. `filter_input(text: str)` is **text-only by signature** |
| `shared/auth/rbac.py` | `Role.ADMIN` is cross-org by construction |
| `k8s/helm/waddleai` | The only supported deployment path |

## Domain-Specific Terms

- **WaddleAI tokens vs LLM tokens** — normalized billing units vs raw provider counts.
- **Tier 1–4** — the content-filter stages: regex/PII → org rules → NER → guard-model
  auditor. **Tiers 1–3 are inherently text-only.**
- **`on_unclassifiable`** — per-modality policy field, default **`reject`**. Distinct
  from `fail_mode: degrade`, which means "the tiers-1–3 verdict stands" and therefore
  silently means *allow* for a modality those tiers cannot read.
- **Dual-default pattern** — every non-OSI weights default must have an Apache-2.0
  alternative selectable in config, wherever one exists that clears the origin policy.

## Known limitation, current release

**Image content bypasses security filtering entirely.** `/v1/messages` preserves
Anthropic content arrays including `image`, but `_extract_text_from_claude_messages`
collects only `type == "text"` items and `filter_input` takes a plain string — so an
image part reaches the provider having passed zero safety tiers. No `image_url` or
`input_audio` handling exists in `proxy/` or `shared/`. See spec §3.6 and §8.3a.
