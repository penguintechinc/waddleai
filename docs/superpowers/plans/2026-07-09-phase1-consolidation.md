# Phase 1 — Consolidation & Platform Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Work on branch `chore/consolidate-quart-k8s`, branched off `release/v0.2.X` (create it off the current release line if it does not exist — house rule: never branch off `main`). Merge back into `release/v0.2.X` without a PR when complete. This is the **first** branch in the §14.1 dependency chain — everything else depends on it.

**Goal:** One management plane, one K8s tree, one deployable chart, license-clean infrastructure — before any new feature lands on it (spec §4). Migrate `services/management/` from Flask to Quart; retire the legacy FastAPI admin plane and the WebSockets MCP server; collapse `infrastructure/kubernetes/` into the canonical `k8s/` tree; add the proxy to the Helm chart; make Valkey/`CACHE_*` the standard; compile hash-pinned requirements; add a `pip-licenses` gate. Golden contract snapshots captured **first** are the merge gate for the whole phase.

**Architecture:** `services/management/` (Flask today: `create_app` factory in `app/__init__.py`, ~13 blueprints under `app/api/v1/*` + `routing_matrix_bp`, `wsgi.py` gunicorn entrypoint, Flask-Security-Too glue in `app/extensions.py`) becomes a Quart app served by hypercorn, authed by penguin-aaa OIDC (auth blueprint already calls `shared.auth.penguin_auth`). `proxy/` is **already** Quart+hypercorn (`proxy/apps/proxy_server/main.py` serving `/v1/chat/completions`, `/v1/messages`, `/v1/models`, and the `/mem0/*` blueprint) — it is touched only by the k8s/deps tasks, and its contract snapshots prove it stays byte-identical. Alembic + SQLAlchemy schema and penguin-dal runtime are unchanged throughout.

**Consolidation is infrastructure hygiene, not a user-facing feature** — no new PostHog flag wraps it (feature flags begin at §5). The standing "flag-off proof" gate (§14.2) does not apply here; the **contract snapshots are the gate** instead.

**Tech Stack:** Python 3.13, Quart 0.19+, hypercorn, penguin-aaa, penguin-dal, SQLAlchemy 2 + Alembic, pytest + pytest-asyncio, httpx, uv (`uv pip compile --generate-hashes`), pip-licenses, Helm v3, Kustomize, Valkey 8 (`valkey/valkey:8-bookworm`, digest-pinned), Debian bookworm images.

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `tests/contract/conftest.py` | `live_server` fixtures (management + proxy) on temp SQLite; upstream-LLM stub |
| Create | `tests/contract/snapshot.py` | Snapshot record/compare helper (normalizes ids/timestamps/tokens) |
| Create | `tests/contract/test_management_contract.py` | Snapshots for all `/api/v1/*` routes |
| Create | `tests/contract/test_proxy_contract.py` | Snapshots for `/v1/chat/completions`, `/v1/messages`, `/v1/models`, `/mem0/*` |
| Create | `tests/contract/snapshots/*.json` | Committed golden snapshots (the merge gate) |
| Modify | `Makefile` | Add `test-contract` target; add `pip-licenses` gate to `test-security` |
| Modify | `services/management/requirements.in` | Drop flask/flask-security-too/gunicorn/gevent; add quart/hypercorn/quart-cors |
| Modify | `services/management/requirements.txt` | Recompiled via `uv pip compile --generate-hashes` |
| Modify | `services/management/app/__init__.py` | Flask factory → Quart factory; async health/metrics/error handlers |
| Modify | `services/management/app/extensions.py` | Remove Flask-Security-Too glue; penguin-aaa OIDC; keep penguin-dal + cache client |
| Modify | `services/management/app/api/v1/*.py` | Blueprint-by-blueprint `def` → `async def`; blocking DB via `asyncio.to_thread` |
| Modify | `services/management/app/api/v1/routing_matrix.py` | Same async conversion (registered separately) |
| Create | `services/management/asgi.py` | Hypercorn ASGI entrypoint (`app`) |
| Delete | `services/management/wsgi.py` | Replaced by `asgi.py` |
| Modify | `services/management/Dockerfile` | CMD hypercorn; drop `FLASK_APP`; native health check |
| Modify | `services/management/app/config.py` | `CACHE_*` env; `REDIS_URL` kept as deprecated alias; drop `MARCHPROXY_AILB_*` |
| Delete | `management/apps/management_server/` | Legacy FastAPI + HTML admin plane |
| Delete | `management/apps/mcp_server/` | Legacy WebSockets MCP server (Q#5: no external consumers) |
| Delete | `shared/utils/mcp_interface.py` | WebSockets MCP interface (dies with legacy plane) |
| Create | `k8s/helm/waddleai/templates/proxy-deployment.yaml` | AIProxy Deployment |
| Create | `k8s/helm/waddleai/templates/proxy-service.yaml` | AIProxy ClusterIP Service |
| Rename | `k8s/helm/waddleai/templates/redis-*.yaml` → `valkey-*.yaml` | Valkey naming; `valkey-cli` probes |
| Modify | `k8s/helm/waddleai/values*.yaml` | `redis:` key → `valkey:`; add `proxy:` block; httproute rules for `/v1/*`,`/mem0/*` |
| Modify | `k8s/helm/waddleai/templates/_helpers.tpl` | `waddleai.proxy.image`; `waddleai.redis.image` → `waddleai.valkey.image` |
| Delete | `infrastructure/kubernetes/` | Legacy tree (contains `redis:7-alpine`) — after parity check |

---

## Task Group A — Golden Contract Snapshots (do this first, §4.1)

### Task A1: Contract-snapshot harness + `make test-contract`

**Files:** Create `tests/contract/conftest.py`, `tests/contract/snapshot.py`, `tests/contract/__init__.py`; Modify `Makefile`.

- [ ] **Step 1: Write the snapshot helper**

Create `tests/contract/snapshot.py`. It compares a live response against a committed JSON snapshot, normalizing volatile fields; records on first run when `CONTRACT_RECORD=1`.

```python
import json
import os
import re
from pathlib import Path

SNAP_DIR = Path(__file__).parent / "snapshots"
_VOLATILE_KEYS = {"id", "created_at", "modified_at", "expires_at", "iat", "exp",
                   "access_token", "token", "key", "api_key", "key_hash", "timestamp"}
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _normalize(obj):
    if isinstance(obj, dict):
        return {k: ("<VOLATILE>" if k in _VOLATILE_KEYS else _normalize(v)) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    if isinstance(obj, str):
        return _UUID.sub("<UUID>", obj)
    return obj


def assert_snapshot(name: str, *, status: int, body):
    SNAP_DIR.mkdir(exist_ok=True)
    path = SNAP_DIR / f"{name}.json"
    actual = {"status": status, "body": _normalize(body)}
    if os.environ.get("CONTRACT_RECORD") == "1" or not path.exists():
        path.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
        return
    expected = json.loads(path.read_text())
    assert actual == expected, f"Contract drift for {name}:\nexpected {expected}\nactual {actual}"
```

- [ ] **Step 2: Write the live-server fixtures**

Create `tests/contract/conftest.py`. Each service is launched by its **own real server** (framework-agnostic — survives the Flask→Quart switch) against a temp SQLite DB, and hit over HTTP with httpx. The upstream LLM connector is stubbed so proxy snapshots are deterministic.

```python
import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait(url, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"server at {url} never became ready")


def _launch(module_app, port, cwd, extra_env=None):
    env = {**os.environ, "DB_TYPE": "sqlite",
           "DATABASE_URL": f"sqlite:///{cwd}/contract.db",
           "FLASK_ENV": "testing", "RELEASE_MODE": "false",
           "CACHE_HOST": "", "REDIS_URL": ""}
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen(
        ["hypercorn", module_app, "--bind", f"127.0.0.1:{port}", "--workers", "1"],
        cwd=str(cwd), env=env,
    )
    _wait(f"http://127.0.0.1:{port}/healthz")
    return proc


@pytest.fixture(scope="session")
def management_url(tmp_path_factory):
    cwd = tmp_path_factory.mktemp("mgmt")
    # Pre-migration this points at wsgi:app; after Task B-final it is asgi:app (same object).
    entry = "asgi:app" if (REPO / "services/management/asgi.py").exists() else "wsgi:app"
    proc = _launch(entry, _port := _free_port(), REPO / "services/management")
    yield f"http://127.0.0.1:{_port}"
    proc.terminate()


@pytest.fixture(scope="session")
def proxy_url(tmp_path_factory):
    proc = _launch("apps.proxy_server.main:app", _port := _free_port(), REPO / "proxy",
                   {"WADDLEAI_STUB_UPSTREAM": "1"})
    yield f"http://127.0.0.1:{_port}"
    proc.terminate()
```

- [ ] **Step 3: Add `make test-contract`**

In `Makefile`, add to `.PHONY` and append a target:

```makefile
test-contract:
	@echo "Running contract snapshot tests..."
	python3 -m pytest tests/contract -v --no-cov
```

- [ ] **Step 4: Verify the harness imports (no snapshots yet)**

```bash
cd /home/penguin/code/waddleai
python3 -c "import tests.contract.snapshot as s; print(s.assert_snapshot)"
```

Expected: prints a function reference, no error.

- [ ] **Step 5: Commit**

```bash
git add tests/contract/__init__.py tests/contract/snapshot.py tests/contract/conftest.py Makefile
git commit -m "test(contract): add golden-snapshot harness and make test-contract target

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task A2: Capture `/api/v1/*` management contract snapshots

**Files:** Create `tests/contract/test_management_contract.py` + committed `snapshots/mgmt_*.json`.

- [ ] **Step 1: Write the snapshot tests**

Cover every management surface: `auth` (login, me, verify, refresh, change-password, logout), `organizations`, `users`, `keys`, `quotas`, `usage`, `providers`, `ollama` + `ollama_models`, `llamacpp`, `ailb` + `ailb_memory`, `routing_matrix`, `webhooks` — plus auth behavior (401 unauth, 403 wrong role) and error formats (400/404 bodies). One representative case per route family:

```python
import httpx


def _login(base):
    r = httpx.post(f"{base}/api/v1/auth/login",
                   json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_auth_login_shape(management_url):
    from tests.contract.snapshot import assert_snapshot
    r = httpx.post(f"{management_url}/api/v1/auth/login",
                   json={"username": "admin", "password": "admin123"})
    assert_snapshot("mgmt_auth_login", status=r.status_code, body=r.json())


def test_auth_login_bad_creds(management_url):
    from tests.contract.snapshot import assert_snapshot
    r = httpx.post(f"{management_url}/api/v1/auth/login",
                   json={"username": "admin", "password": "wrong"})
    assert_snapshot("mgmt_auth_login_bad", status=r.status_code, body=r.json())


def test_orgs_list_requires_auth(management_url):
    from tests.contract.snapshot import assert_snapshot
    r = httpx.get(f"{management_url}/api/v1/organizations")
    assert_snapshot("mgmt_orgs_unauth", status=r.status_code, body=r.json())


def test_orgs_list(management_url):
    from tests.contract.snapshot import assert_snapshot
    r = httpx.get(f"{management_url}/api/v1/organizations", headers=_login(management_url))
    assert_snapshot("mgmt_orgs_list", status=r.status_code, body=r.json())

# ... one test per route family: users, keys, quotas, usage, providers,
#     ollama/deployments, ollama/models, llamacpp/deployments, ailb/status,
#     ailb/memory-config, routing_matrix (GET /), webhooks, plus a 404 case.
```

- [ ] **Step 2: Record snapshots against the current (Flask) service**

```bash
cd /home/penguin/code/waddleai
CONTRACT_RECORD=1 python3 -m pytest tests/contract/test_management_contract.py -v --no-cov
```

Expected: all pass; `tests/contract/snapshots/mgmt_*.json` written.

- [ ] **Step 3: Re-run without record to prove stability**

```bash
python3 -m pytest tests/contract/test_management_contract.py -v --no-cov
```

Expected: all pass against committed snapshots.

- [ ] **Step 4: Commit**

```bash
git add tests/contract/test_management_contract.py tests/contract/snapshots/mgmt_*.json
git commit -m "test(contract): capture /api/v1/* management golden snapshots (pre-migration)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task A3: Capture proxy contract snapshots

**Files:** Create `tests/contract/test_proxy_contract.py` + `snapshots/proxy_*.json`; add upstream stub honoring `WADDLEAI_STUB_UPSTREAM`.

- [ ] **Step 1: Add a deterministic upstream stub in the proxy**

In `proxy/apps/proxy_server/main.py`, gate the dispatch connector so that when `WADDLEAI_STUB_UPSTREAM=1` a fixed completion/usage is returned instead of a real provider call (test-only; no behavior change in production). Keep the response envelope identical to the real path.

- [ ] **Step 2: Write snapshot tests** for `/v1/models`, `/v1/chat/completions` (non-streaming), `/v1/messages` (non-streaming), and the `/mem0/memories` GET/POST/DELETE surface; include a 401 unauth case and a 400 malformed-body case per endpoint.

- [ ] **Step 3: Record + verify**

```bash
cd /home/penguin/code/waddleai
CONTRACT_RECORD=1 python3 -m pytest tests/contract/test_proxy_contract.py -v --no-cov
python3 -m pytest tests/contract/test_proxy_contract.py -v --no-cov
```

Expected: recorded then green.

- [ ] **Step 4: Full contract gate green**

```bash
make test-contract
```

Expected: management + proxy snapshots all pass.

- [ ] **Step 5: Commit**

```bash
git add proxy/apps/proxy_server/main.py tests/contract/test_proxy_contract.py tests/contract/snapshots/proxy_*.json
git commit -m "test(contract): capture /v1/* and /mem0/* proxy golden snapshots

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task Group B — Flask → Quart Management Migration (§4.2)

> After **every** task in this group, `make test-contract` must stay green — the snapshots recorded in A2 are the proof the migration changed nothing observable.

### Task B1: Swap web-framework dependencies

**Files:** Modify `services/management/requirements.in`.

- [ ] **Step 1:** In `services/management/requirements.in` remove `flask>=3.0.0`, `flask-security-too>=5.3.0`, `flask-cors>=4.0.0`, `flask-limiter>=3.5.0`, `gunicorn>=21.2.0`, `gevent>=23.9.0`. Add:

```
# Web framework (Quart — async-native Flask superset, house standard)
quart>=0.19.0
quart-cors>=0.7.0
hypercorn>=0.16.0
```

Keep `redis>=5.0.0`, `penguin-aaa`, `penguin-dal`, `sqlalchemy`, all provider SDKs, `tiktoken`, `pyyaml`.

- [ ] **Step 2: Compile pinned hashes** (final cross-service recompile happens in Task E1):

```bash
cd /home/penguin/code/waddleai/services/management
uv pip compile requirements.in --generate-hashes -o requirements.txt
```

- [ ] **Step 3: Commit**

```bash
git add services/management/requirements.in services/management/requirements.txt
git commit -m "chore(management): swap Flask stack for Quart+hypercorn deps

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task B2: Convert the application factory to Quart

**Files:** Modify `services/management/app/__init__.py`.

- [ ] **Step 1:** Replace `from flask import Flask, Response` with `from quart import Quart, Response`; `app = Flask(__name__)` → `app = Quart(__name__)`. Convert `create_app`, `register_blueprints`, `register_error_handlers`, and the inline `healthz`/`readyz`/`livez`/`metrics` routes to `async def`. Replace `flask_cors.CORS` with `quart_cors.cors(app, allow_origin=...)`. `_auto_register_k8s_ollama` runs blocking DB work — wrap its body call site in `await asyncio.to_thread(...)` from within an async `@app.before_serving` hook (Quart lifecycle), not at import time.

- [ ] **Step 2: Verify the app imports and boots**

```bash
cd /home/penguin/code/waddleai/services/management
python3 -c "import asyncio; from app import create_app; print(type(create_app()).__name__)"
```

Expected: `Quart`.

- [ ] **Step 3:** `make test-contract` (management fixture now boots the Quart factory). Expected: green.

- [ ] **Step 4: Commit**

```bash
git add services/management/app/__init__.py
git commit -m "refactor(management): convert application factory to Quart

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task B3: Replace Flask-Security-Too glue with penguin-aaa

**Files:** Modify `services/management/app/extensions.py`.

- [ ] **Step 1:** Delete the `PyDALUserDatastore`, `PyDALUser`, `PyDALRole`, and `init_security` code (Flask-Security-Too). Auth already flows through `shared.auth.penguin_auth` (see `app/api/v1/auth.py`) — the OIDC provider is the authority. Keep `init_db` (penguin-dal), `init_redis` (rename in Task D4), and `init_default_data`. Update `init_extensions` to drop the `security` global. Remove `from flask_security...` imports; change `from flask import Flask` → `from quart import Quart` and type hints accordingly.

- [ ] **Step 2:** Grep-verify no Flask-Security symbols remain:

```bash
cd /home/penguin/code/waddleai
grep -rn "flask_security\|Flask-Security\|PyDALUserDatastore" services/management/app/ || echo "clean"
```

Expected: `clean`.

- [ ] **Step 3:** `make test-contract`. Expected: green (auth snapshots from A2 unchanged).

- [ ] **Step 4: Commit**

```bash
git add services/management/app/extensions.py
git commit -m "refactor(management): drop Flask-Security-Too glue; penguin-aaa OIDC is authoritative

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task B4: Convert auth/identity blueprints to async

**Files:** Modify `app/api/v1/auth.py`, `users.py`, `organizations.py`, `keys.py`.

- [ ] **Step 1:** For each blueprint: change `from flask import ...` → `from quart import ...`; make every route handler `async def`; `request.get_json()` → `await request.get_json()`; wrap blocking penguin-dal calls in `await asyncio.to_thread(lambda: <db call>)` per house async rules (until penguin-dal async paths are wired). Keep route paths, status codes, and JSON bodies identical.

- [ ] **Step 2:** `make test-contract` — auth/orgs/users/keys snapshots prove parity. Expected: green.

- [ ] **Step 3: Commit**

```bash
git add services/management/app/api/v1/auth.py services/management/app/api/v1/users.py services/management/app/api/v1/organizations.py services/management/app/api/v1/keys.py
git commit -m "refactor(management): convert auth/users/orgs/keys blueprints to async Quart

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task B5: Convert quota/usage/provider/webhook blueprints to async

**Files:** Modify `app/api/v1/quotas.py`, `usage.py`, `providers.py`, `webhooks.py`.

- [ ] **Step 1:** Same conversion pattern as B4 (Flask→Quart imports, `async def`, `await request.get_json()`, `asyncio.to_thread` for blocking DB).
- [ ] **Step 2:** `make test-contract`. Expected: green.
- [ ] **Step 3: Commit**

```bash
git add services/management/app/api/v1/quotas.py services/management/app/api/v1/usage.py services/management/app/api/v1/providers.py services/management/app/api/v1/webhooks.py
git commit -m "refactor(management): convert quota/usage/provider/webhook blueprints to async Quart

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task B6: Convert fleet/routing blueprints to async

**Files:** Modify `app/api/v1/ollama.py`, `ollama_models.py`, `llamacpp.py`, `ailb.py`, `ailb_memory.py`, `routing_matrix.py`.

- [ ] **Step 1:** Same conversion pattern. `ailb.py`/`ailb_memory.py` are slated for deletion/re-home in §5 — convert them here only enough to keep their contract snapshots green (they must survive Phase 1 unchanged). `routing_matrix_bp` is registered separately in `app/__init__.py`; keep that registration.
- [ ] **Step 2:** `make test-contract`. Expected: green.
- [ ] **Step 3: Commit**

```bash
git add services/management/app/api/v1/ollama.py services/management/app/api/v1/ollama_models.py services/management/app/api/v1/llamacpp.py services/management/app/api/v1/ailb.py services/management/app/api/v1/ailb_memory.py services/management/app/api/v1/routing_matrix.py
git commit -m "refactor(management): convert fleet/routing blueprints to async Quart

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task B7: Hypercorn entrypoint + Dockerfile CMD

**Files:** Create `services/management/asgi.py`; Delete `services/management/wsgi.py`; Modify `services/management/Dockerfile`.

- [ ] **Step 1:** Create `asgi.py` exposing `app` (same env-based config selection as the old `wsgi.py`), e.g.:

```python
"""WaddleAI Management Service — ASGI entrypoint (hypercorn)."""
import os
from app import create_app

_env = os.environ.get("FLASK_ENV", "production")
if _env == "development":
    from app.config import DevelopmentConfig as _Cfg
elif _env == "testing":
    from app.config import TestingConfig as _Cfg
else:
    from app.config import ProductionConfig as _Cfg

app = create_app(_Cfg)
```

- [ ] **Step 2:** `git rm services/management/wsgi.py`.

- [ ] **Step 3:** In `services/management/Dockerfile`: replace the gunicorn `CMD` with hypercorn, drop `FLASK_APP` from `ENV` (keep `PYTHONUNBUFFERED`/`PYTHONDONTWRITEBYTECODE`), copy `asgi.py` instead of `wsgi.py`, and switch the health check to a native Python probe (no `curl`):

```dockerfile
COPY asgi.py /app/
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/healthz')" || exit 1
CMD ["hypercorn", "asgi:app", "--bind", "0.0.0.0:8001", "--workers", "2"]
```

- [ ] **Step 4: Full-suite + contract gate**

```bash
cd /home/penguin/code/waddleai
make test-contract
grep -rn "import flask\|from flask\|flask_security\|gunicorn\|gevent" services/management/ --include=*.py || echo "no flask refs"
```

Expected: contract green; **no flask refs** (§4 acceptance: zero `flask` references in management).

- [ ] **Step 5: Commit**

```bash
git add services/management/asgi.py services/management/Dockerfile
git rm services/management/wsgi.py
git commit -m "refactor(management): hypercorn ASGI entrypoint; drop gunicorn/wsgi

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task Group C — Retire the Legacy FastAPI Plane (§4.3)

### Task C1: Port routing-config + memory-config admin surfaces to `/api/v1` (audit before delete)

**Files:** Modify `app/api/v1/routing_matrix.py` / `app/api/v1/ailb_memory.py` as needed; extend `tests/contract/test_management_contract.py`.

- [ ] **Step 1: Audit** what the legacy HTML admin pages do that `/api/v1` does not. The legacy plane (`management/apps/management_server/main.py`) serves HTML for: `routing-config`, `providers`, `memory-config`, `redis-config`, `mcp-management`, `analytics`, `performance`, `dashboard`. Per §4.3 the only surfaces to preserve are **routing-config** and **memory-config**; the rest are superseded (providers/analytics/dashboard already have `/api/v1` + WebUI equivalents; redis-config/performance/mcp-management are obsolete). Map each field the two HTML pages read/write to an existing `/api/v1` route:
  - routing-config → `routing_matrix_bp` (`GET/POST /`, `PUT/DELETE /<id>`) and `ailb` routing endpoints.
  - memory-config → `ailb_memory` (`/ailb/memory-config`, `/ailb/rag-config`, `/ailb/embedding-config`).

- [ ] **Step 2:** Add any missing field to the corresponding `/api/v1` route so the WebUI has full parity (if the audit finds nothing missing, record that in the commit message). Extend the contract tests to snapshot the routing-config and memory-config GET payloads.

- [ ] **Step 3:** If a `webui/` service exists in the repo, add React screens ("Routing" and "Memory") that call these `/api/v1` endpoints (thin presentation layer, per house frontend standards). If no `webui/` tree is present in this repo, note it — the WebUI lives in its own deployment and the API parity delivered here is the contract it consumes.

- [ ] **Step 4:** Record new snapshots + verify:

```bash
cd /home/penguin/code/waddleai
CONTRACT_RECORD=1 python3 -m pytest tests/contract/test_management_contract.py -k "routing_config or memory_config" --no-cov
make test-contract
```

- [ ] **Step 5: Commit**

```bash
git add services/management/app/api/v1/ tests/contract/
git commit -m "feat(management): ensure /api/v1 parity for routing-config and memory-config admin surfaces

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task C2: Delete the legacy FastAPI management server

**Files:** Delete `management/apps/management_server/`.

- [ ] **Step 1: Confirm nothing imports it**

```bash
cd /home/penguin/code/waddleai
grep -rn "management_server\|apps.management_server" --include=*.py --include=*.yaml . | grep -v "management/apps/management_server/" || echo "no external refs"
```

Expected: `no external refs`.

- [ ] **Step 2: Delete**

```bash
git rm -r management/apps/management_server
```

- [ ] **Step 3: Full suite + contract gate**

```bash
make test-contract && python3 -m pytest tests/unit --no-cov 2>&1 | tail -5
```

Expected: green (no test depended on the FastAPI plane).

- [ ] **Step 4: Commit**

```bash
git commit -m "chore: delete legacy FastAPI management plane (superseded by Quart /api/v1 + WebUI)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task C3: Delete the WebSockets MCP server and interface

**Files:** Delete `management/apps/mcp_server/`, `shared/utils/mcp_interface.py`.

- [ ] **Step 1: Confirm no live consumers** (Q#5: VS Code extension is pure REST; only the deleted FastAPI plane + an example script referenced it)

```bash
cd /home/penguin/code/waddleai
grep -rn "mcp_interface\|create_mcp_server\|mcp_server" --include=*.py . | grep -v "management/apps/mcp_server/\|shared/utils/mcp_interface.py" || echo "no live consumers"
```

Expected: `no live consumers` (fix any straggler import found in a test/example before deleting).

- [ ] **Step 2: Delete**

```bash
git rm -r management/apps/mcp_server shared/utils/mcp_interface.py
```

- [ ] **Step 3: Verify + contract gate**

```bash
make test-contract && python3 -m pytest tests/unit --no-cov 2>&1 | tail -5
```

- [ ] **Step 4: Commit**

```bash
git commit -m "chore: delete legacy WebSockets MCP server + interface (Q#5: no external consumers)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task Group D — One Kubernetes Tree (§4.4)

### Task D1: Add the AIProxy to the Helm chart

**Files:** Create `k8s/helm/waddleai/templates/proxy-deployment.yaml`, `proxy-service.yaml`; Modify `_helpers.tpl`, `values.yaml`, `values-alpha.yaml`, `values-beta.yaml`.

- [ ] **Step 1:** Add a `proxy:` block to `values.yaml` (image `repository`/`tag`/`digest`, `replicaCount`, `service.port: 8080` + gRPC `50051`, resource tier per house standard, health probes `/healthz` + `/readyz`), plus a `waddleai.proxy.image` helper in `_helpers.tpl` mirroring `waddleai.management.image`.

- [ ] **Step 2:** Create `proxy-deployment.yaml` (guarded by `.Values.proxy.enabled`) — container port 8080 + 50051, `securityContext` per house standard (`runAsNonRoot: true`, `runAsUser: 1000`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `capabilities.drop: [ALL]`), liveness/readiness probes, resource limits, `CACHE_*` + `DATABASE_URL` env from the shared secret/config. Create `proxy-service.yaml` (ClusterIP, ports 8080 + 50051).

- [ ] **Step 3: Golden render**

```bash
cd /home/penguin/code/waddleai
helm template waddleai k8s/helm/waddleai -f k8s/helm/waddleai/values-beta.yaml --set proxy.enabled=true > /tmp/proxy-render.yaml
grep -q "kind: Deployment" /tmp/proxy-render.yaml && grep -q "name: waddleai-proxy" /tmp/proxy-render.yaml && echo OK
helm lint k8s/helm/waddleai
```

Expected: `OK`; lint passes.

- [ ] **Step 4: Commit**

```bash
git add k8s/helm/waddleai/templates/proxy-deployment.yaml k8s/helm/waddleai/templates/proxy-service.yaml k8s/helm/waddleai/templates/_helpers.tpl k8s/helm/waddleai/values.yaml k8s/helm/waddleai/values-alpha.yaml k8s/helm/waddleai/values-beta.yaml
git commit -m "feat(helm): deploy AIProxy (deployment + service + values + image helper)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task D2: HTTPRoute rules for `/v1/*` and `/mem0/*`

**Files:** Modify `k8s/helm/waddleai/values.yaml`, `values-beta.yaml`.

- [ ] **Step 1:** Populate `httproute.rules` so `/v1/` and `/mem0/` path prefixes route to the proxy Service (port 8080) and `/api/v1/` continues to the management Service. Set `httproute.enabled: true` in `values-beta.yaml` with the beta host.

```yaml
httproute:
  enabled: true
  rules:
    - path: /v1/
      service: waddleai-proxy
      port: 8080
      timeoutSeconds: 600
    - path: /mem0/
      service: waddleai-proxy
      port: 8080
    - path: /api/v1/
      service: waddleai-management
      port: 8001
```

- [ ] **Step 2: Render check**

```bash
cd /home/penguin/code/waddleai
helm template waddleai k8s/helm/waddleai -f k8s/helm/waddleai/values-beta.yaml | grep -A2 "value: /v1/" && echo OK
```

- [ ] **Step 3: Commit**

```bash
git add k8s/helm/waddleai/values.yaml k8s/helm/waddleai/values-beta.yaml
git commit -m "feat(helm): route /v1/* and /mem0/* to AIProxy via HTTPRoute

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task D3: Rename redis templates → valkey

**Files:** Rename `redis-deployment.yaml`→`valkey-deployment.yaml`, `redis-service.yaml`→`valkey-service.yaml`, `redis-pvc.yaml`→`valkey-pvc.yaml`; Modify `_helpers.tpl`, `values*.yaml`, `management-deployment.yaml`.

- [ ] **Step 1:** `git mv` the three templates to `valkey-*.yaml`. Inside them, rename the `redis` container/labels/component to `valkey`, and change the liveness/readiness probe commands from `redis-cli` to `valkey-cli` (the image is already `valkey/valkey:8-bookworm`, digest-pinned). Rename the `values.yaml` key `redis:` → `valkey:` (image block already Valkey) and the helper `waddleai.redis.image` → `waddleai.valkey.image`. Update `management-deployment.yaml` service references from `-redis` to `-valkey`.

- [ ] **Step 2: Render + lint**

```bash
cd /home/penguin/code/waddleai
helm template waddleai k8s/helm/waddleai -f k8s/helm/waddleai/values-beta.yaml | grep -i "valkey-cli" && echo OK
grep -rn "redis-cli\|name: redis\b" k8s/helm/waddleai/ || echo "no redis-cli left"
helm lint k8s/helm/waddleai
```

Expected: `OK`; no `redis-cli` left; lint passes.

- [ ] **Step 3: Commit**

```bash
git add k8s/helm/waddleai/
git commit -m "chore(helm): rename redis templates/values to valkey; valkey-cli probes

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task D4: `CACHE_*` env with `REDIS_URL` deprecated alias

**Files:** Modify `services/management/app/config.py`, `app/extensions.py`, Helm `management-deployment.yaml` + `proxy-deployment.yaml` env, and (if present) the proxy cache config.

- [ ] **Step 1:** In `config.py`, add `CACHE_HOST`, `CACHE_PORT`, `CACHE_USER`, `CACHE_PASS` (house standard) and derive a cache URL from them; keep `REDIS_URL` as a **deprecated alias** honored for one release (log a deprecation warning when it is the source). In `extensions.py`, rename `init_redis`→`init_cache` (keep a `redis_client` alias attribute so nothing else breaks this release) reading the new vars.

- [ ] **Step 2:** Update Helm deployment env blocks to set `CACHE_HOST` (the valkey Service) and keep a `REDIS_URL` alias env for one release.

- [ ] **Step 3: Verify + contract gate**

```bash
cd /home/penguin/code/waddleai
python3 -c "from services.management.app.config import Config; print(hasattr(Config,'CACHE_HOST'))"
make test-contract
```

Expected: `True`; contract green.

- [ ] **Step 4: Commit**

```bash
git add services/management/app/config.py services/management/app/extensions.py k8s/helm/waddleai/templates/management-deployment.yaml k8s/helm/waddleai/templates/proxy-deployment.yaml
git commit -m "refactor: adopt CACHE_* env; keep REDIS_URL as deprecated alias (one release)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task D5: Delete `infrastructure/kubernetes/` after parity check

**Files:** Delete `infrastructure/kubernetes/`; Modify `k8s/` if the parity check finds anything worth merging.

- [ ] **Step 1: Object-by-object parity check** — for each object in `infrastructure/kubernetes/base/` (configmap, ingress, namespace, secret, postgres/management/webui deployments, `redis-deployment.yaml` with `redis:7-alpine`), confirm `k8s/helm/waddleai/` (or Kustomize base) has an equivalent. Merge any still-relevant configmap/ingress content into `k8s/`. The `redis:7-alpine` object is **not** merged — it dies with the tree (§2.1 license fix).

- [ ] **Step 2: Delete the tree**

```bash
cd /home/penguin/code/waddleai
git rm -r infrastructure/kubernetes
```

- [ ] **Step 3: Digest-pin + image audit** across remaining Dockerfiles/manifests (external images carry `@sha256:`; Python images are `python:3.13-slim-bookworm`):

```bash
grep -rn "image:" k8s/ | grep -v "@sha256:" | grep -vE "waddleai\.(management|webui|proxy)\.image|include " || echo "all external images digest-pinned"
grep -rn "redis:7\|redis:.*alpine\|infrastructure/kubernetes" . --include=*.yaml --include=*.sh --include=*.md || echo "no legacy redis/infra refs"
```

Expected: all external images digest-pinned; no legacy redis/infra references.

- [ ] **Step 4: Commit**

```bash
git commit -m "chore(k8s): delete infrastructure/kubernetes tree (parity merged; redis:7-alpine retired)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task Group E — Dependency Hygiene (§4.5)

### Task E1: Compile hash-pinned requirements for both services

**Files:** Modify `services/management/requirements.txt`, `proxy/requirements.txt` (from their `.in` files).

- [ ] **Step 1:** Drop dependencies the Flask→Quart migration and legacy-plane deletion made unused (e.g. `flask-*`, `gunicorn`, `gevent` already gone; audit `cohere`, `docker`, unused gRPC extras against actual imports). Recompile both with hashes:

```bash
cd /home/penguin/code/waddleai
( cd services/management && uv pip compile requirements.in --generate-hashes -o requirements.txt )
( cd proxy && uv pip compile requirements.in --generate-hashes -o requirements.txt )
```

- [ ] **Step 2: Verify hashes present**

```bash
grep -c "\--hash=sha256:" services/management/requirements.txt proxy/requirements.txt
```

Expected: both counts > 0.

- [ ] **Step 3: Commit**

```bash
git add services/management/requirements.in services/management/requirements.txt proxy/requirements.in proxy/requirements.txt
git commit -m "chore(deps): recompile hash-pinned requirements; drop unused deps

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task E2: Add `pip-licenses` gate to `make test-security`

**Files:** Modify `Makefile`; Create `scripts/check-licenses.sh`.

- [ ] **Step 1:** Create `scripts/check-licenses.sh` that runs `pip-licenses --format=json` and fails on any non-OSI / forbidden license (AGPL third-party, SSPL, BUSL, Elastic, RSAL, Commons Clause, CC-BY-NC) per §2.1. Make it a real gate (non-zero exit on violation), listing offenders.

- [ ] **Step 2:** In `Makefile` `test-security`, add:

```makefile
	@echo "-- pip-licenses (OSI gate) --"; bash scripts/check-licenses.sh
```

- [ ] **Step 3: Run the gate**

```bash
cd /home/penguin/code/waddleai
bash scripts/check-licenses.sh && echo "licenses clean"
```

Expected: `licenses clean` (fix or replace any offender before proceeding).

- [ ] **Step 4: Commit**

```bash
git add Makefile scripts/check-licenses.sh
git commit -m "chore(security): add pip-licenses OSI gate to make test-security

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task F: Phase-1 Acceptance Gate (§4 acceptance)

**Files:** none (verification + final sign-off commit if any residue found).

- [ ] **Step 1: Contract snapshots green**

```bash
cd /home/penguin/code/waddleai
make test-contract
```

- [ ] **Step 2: Clean alpha deploy (Kustomize) + smoke test**

```bash
kubectl --context local-alpha kustomize k8s/kustomize/overlays/alpha >/dev/null && echo "kustomize builds"
make smoke-test
```

- [ ] **Step 3: `helm template` golden**

```bash
helm template waddleai k8s/helm/waddleai -f k8s/helm/waddleai/values-beta.yaml > /tmp/waddleai-golden.yaml && echo "helm renders"
helm lint k8s/helm/waddleai
```

- [ ] **Step 4: Zero forbidden references** (the hard §4 acceptance)

```bash
echo "-- flask (management) --"; grep -rn "import flask\|from flask\|flask_security" services/management/ --include=*.py || echo OK
echo "-- fastapi (non-PenguinCode) --"; grep -rn "fastapi" --include=*.py . | grep -v "services/penguincode/" || echo OK
echo "-- redis: images --"; grep -rn "image:.*redis" k8s/ || echo OK
echo "-- infrastructure/kubernetes --"; test -d infrastructure/kubernetes && echo "STILL PRESENT" || echo OK
```

Expected: `OK` on every line. Any residue is a bug in a prior task — fix it, do not wave it through.

- [ ] **Step 5: Cross-arch + container checks** (standing gate §14.2) — rootless, digest-pinned, Debian bookworm on `services/management/Dockerfile`, `proxy/Dockerfile`, and Helm images:

```bash
grep -rn "USER " services/management/Dockerfile proxy/Dockerfile
hadolint services/management/Dockerfile proxy/Dockerfile 2>/dev/null || echo "install hadolint to lint"
```

- [ ] **Step 6: Final commit (only if Step 4 required a residue fix)**

```bash
git add -A
git commit -m "chore: Phase 1 consolidation acceptance — contract green, one k8s tree, zero flask/fastapi/redis residue

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-Review Against Spec §4

| Spec requirement (§4) | Task |
|---|---|
| §4.1 Contract snapshots first, `tests/contract/` + `make test-contract` | A1 |
| §4.1 Snapshots for `/api/v1/*` (status, shapes, auth, errors) | A2 |
| §4.1 Snapshots for `/v1/chat/completions`, `/v1/messages`, `/v1/models`, `/mem0/*` | A3 |
| §4.1 Snapshots are the phase merge gate (pass unchanged post-migration) | verified in B2–B7, C1–C3, D4, F1 |
| §4.2 Blueprint-by-blueprint `async def` conversion | B4, B5, B6 |
| §4.2 hypercorn replaces gunicorn/werkzeug; Dockerfile CMD | B1, B7 |
| §4.2 penguin-aaa OIDC replaces Flask-specific auth glue | B3 |
| §4.2 Alembic/SQLAlchemy/penguin-dal unchanged | untouched across B |
| §4.3 Delete `management/apps/management_server/` after audit | C1, C2 |
| §4.3 Port routing-config + memory-config to WebUI-against-`/api/v1` | C1 |
| §4.3 Delete WebSockets `management/apps/mcp_server/` + `shared/utils/mcp_interface.py` (Q#5) | C3 |
| §4.4 Helm gains proxy: `proxy-deployment.yaml`, `proxy-service.yaml`, values | D1 |
| §4.4 HTTPRoute rules for `/v1/*` and `/mem0/*` | D2 |
| §4.4 `redis-*` → `valkey-*` (`valkey/valkey:8-bookworm`, digest-pinned) | D3 |
| §4.4 `CACHE_*` env, `REDIS_URL` deprecated alias one release | D4 |
| §4.4 Delete `infrastructure/kubernetes/` after parity; retire `redis:7-alpine` | D5 |
| §4.4 Digest-pin audit; `python:3.13-slim-bookworm` | D5, F5 |
| §4.5 `uv pip compile --generate-hashes` per service; drop unused deps | B1, E1 |
| §4.5 `pip-licenses` gate in `make test-security` | E2 |
| §4 Acceptance: contract green; clean alpha smoke; helm golden; zero flask/fastapi/redis:/infra refs | F |
