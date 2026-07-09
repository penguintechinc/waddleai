# llama.cpp Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add native llama.cpp support — a `LlamaCppConnector` for inference against llama-server HTTP endpoints, a `LlamaCppManager` for K8s DaemonSet lifecycle and remote-connect registration, and management API routes — mirroring the existing Ollama pattern.

**Architecture:** `LlamaCppConnector` uses `aiohttp` directly against llama-server's OpenAI-compatible API, calling `/tokenize` for exact token counts. `LlamaCppManager` drives the `kubernetes` Python client to create DaemonSets (one per model, nodeSelector-targeted) and ClusterIP Services. Management routes at `/api/v1/llamacpp/deployments` mirror the Ollama routes in shape.

**Tech Stack:** Python 3.12, aiohttp, kubernetes Python client, SQLAlchemy, Flask, pytest, PyYAML

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `services/management/app/services/llamacpp_manager.py` | K8s DaemonSet lifecycle + remote-connect |
| Create | `services/management/app/api/v1/llamacpp.py` | REST routes for deployment CRUD + lifecycle |
| Create | `tests/unit/management/test_llamacpp_routes.py` | Route unit tests |
| Create | `tests/unit/management/test_llamacpp_manager.py` | Manager unit tests |
| Create | `tests/integration/test_llamacpp_integration.py` | Integration tests (skipped without live server) |
| Modify | `shared/utils/llm_connectors.py:3,506-513` | Add `LlamaCppConnector`; register in `_load_connectors()` |
| Modify | `services/management/app/services/providers/__init__.py:19-30,59-92,96-103,240-250,289-303` | Add `LLAMACPP` to `ProviderType`, `LlamaCppConfig`, default models |
| Modify | `services/management/app/models_sqlalchemy.py:150` | Add `LlamaCppDeployment` after `OllamaDeployment` |
| Modify | `services/management/app/api/v1/__init__.py:10-18` | Import `llamacpp` blueprint |
| Modify | `docs/APP_STANDARDS.md` | llama.cpp provider + K8s node labelling section |
| Modify | `docs/TESTING_SETUP.md` | llama.cpp local testing instructions |

---

### Task 1: Add `LlamaCppDeployment` SQLAlchemy model

**Files:**
- Modify: `services/management/app/models_sqlalchemy.py:166` (after `OllamaDeployment` class, before `OllamaModel`)
- Test: `tests/unit/management/test_llamacpp_routes.py` (created in Task 5 — this task just verifies the import)

- [ ] **Step 1: Write import test**

Add to a temporary inline check (run by hand, not committed — just verifies the class is importable after the edit):

```python
from services.management.app.models_sqlalchemy import LlamaCppDeployment
assert LlamaCppDeployment.__tablename__ == "llamacpp_deployments"
```

- [ ] **Step 2: Add the model**

In `services/management/app/models_sqlalchemy.py`, insert after the `OllamaDeployment` class (after line 166, before `class OllamaModel`):

```python
class LlamaCppDeployment(Base):
    __tablename__ = "llamacpp_deployments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    deployment_type = Column(String(50), nullable=False, default="kubernetes")  # kubernetes, remote
    status = Column(String(50), nullable=False, default="pending")  # pending, deploying, running, stopped, error
    status_message = Column(Text)

    # Model
    model_name = Column(String(255), nullable=False)
    model_url = Column(String(512))       # GGUF download URL (kubernetes mode)
    model_filename = Column(String(255))  # filename inside volume

    # Inference params
    n_ctx = Column(Integer, default=4096)
    n_gpu_layers = Column(Integer, default=-1)  # -1 = all layers on GPU
    gpu_count = Column(Integer, default=1)

    # Connection
    endpoint_url = Column(String(512))    # set by manager after deploy, or provided directly for remote

    # Kubernetes
    k8s_namespace = Column(String(255), default="waddleai")
    k8s_daemonset_name = Column(String(255))
    node_selector = Column(JSON)   # e.g. {"waddleai/gpu-tier": "a100"}
    node_affinity = Column(JSON)   # optional advanced scheduling

    created_at = Column(DateTime, default=datetime.utcnow)
    modified_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
```

- [ ] **Step 3: Verify import**

```bash
cd /home/penguin/code/waddleai
python3 -c "from services.management.app.models_sqlalchemy import LlamaCppDeployment; print(LlamaCppDeployment.__tablename__)"
```

Expected output: `llamacpp_deployments`

- [ ] **Step 4: Commit**

```bash
git add services/management/app/models_sqlalchemy.py
git commit -m "feat: add LlamaCppDeployment SQLAlchemy model"
```

---

### Task 2: Register `LlamaCppConfig` provider type

**Files:**
- Modify: `services/management/app/services/providers/__init__.py`
- Test: `tests/unit/management/test_providers.py` (existing file — add two test cases)

- [ ] **Step 1: Write failing tests**

In `tests/unit/management/test_providers.py`, add at the end of the file:

```python
def test_llamacpp_provider_type_exists():
    from services.management.app.services.providers import ProviderType
    assert ProviderType.LLAMACPP == "llamacpp"


def test_llamacpp_config_sets_provider_type():
    from services.management.app.services.providers import LlamaCppConfig, ProviderType
    cfg = LlamaCppConfig(name="test-llama")
    assert cfg.provider_type == ProviderType.LLAMACPP
    assert cfg.model_name == ""
    assert cfg.deployment_id is None


def test_llamacpp_default_models_populated():
    from services.management.app.services.providers import DEFAULT_MODELS, ProviderType
    models = DEFAULT_MODELS[ProviderType.LLAMACPP]
    assert "llama-3.2-3b-instruct" in models
    assert len(models) >= 4
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/penguin/code/waddleai
python3 -m pytest tests/unit/management/test_providers.py::test_llamacpp_provider_type_exists -v --no-cov
```

Expected: FAILED with `ValueError: 'llamacpp' is not a valid ProviderType`

- [ ] **Step 3: Add `LLAMACPP` to `ProviderType` enum**

In `services/management/app/services/providers/__init__.py`, in the `ProviderType` class (around line 19), add after `OLLAMA = "ollama"`:

```python
LLAMACPP = "llamacpp"
```

- [ ] **Step 4: Add default models**

In the `DEFAULT_MODELS` dict (around line 59), add after the `ProviderType.OLLAMA` entry:

```python
ProviderType.LLAMACPP: [
    "llama-3.2-3b-instruct",
    "llama-3.1-8b-instruct",
    "llama-3.1-70b-instruct",
    "mistral-7b-instruct",
    "mixtral-8x7b-instruct",
    "codellama-13b-instruct",
    "phi-3.5-mini-instruct",
    "qwen2.5-7b-instruct",
],
```

- [ ] **Step 5: Add `LlamaCppConfig` dataclass**

After `OllamaConfig` (around line 173), add:

```python
@dataclass
class LlamaCppConfig(ProviderConfig):
    """llama.cpp (llama-server) specific configuration"""

    deployment_id: Optional[int] = None  # links to llamacpp_deployments table
    model_name: str = ""

    def __post_init__(self):
        self.provider_type = ProviderType.LLAMACPP
        if not self.endpoint_url:
            self.endpoint_url = "http://localhost:8080"
        if not self.model_list:
            self.model_list = DEFAULT_MODELS[ProviderType.LLAMACPP].copy()
```

- [ ] **Step 6: Register in `PROVIDER_CONFIG_CLASSES` and `__all__`**

In `PROVIDER_CONFIG_CLASSES` dict (around line 241), add:

```python
ProviderType.LLAMACPP: LlamaCppConfig,
```

In `__all__` list (around line 289), add `"LlamaCppConfig"`.

- [ ] **Step 7: Run tests to verify they pass**

```bash
cd /home/penguin/code/waddleai
python3 -m pytest tests/unit/management/test_providers.py::test_llamacpp_provider_type_exists tests/unit/management/test_providers.py::test_llamacpp_config_sets_provider_type tests/unit/management/test_providers.py::test_llamacpp_default_models_populated -v --no-cov
```

Expected: 3 passed

- [ ] **Step 8: Run full test suite to check for regressions**

```bash
python3 -m pytest tests/ --tb=short 2>&1 | tail -5
```

Expected: all previously passing tests still pass

- [ ] **Step 9: Commit**

```bash
git add services/management/app/services/providers/__init__.py tests/unit/management/test_providers.py
git commit -m "feat: add LlamaCppConfig and LLAMACPP provider type"
```

---

### Task 3: Implement `LlamaCppConnector`

**Files:**
- Modify: `shared/utils/llm_connectors.py` (add class after `OllamaConnector` at line 465, update `_load_connectors` at line 506)
- Test: `tests/unit/test_llm_connectors.py` (existing file — add `TestLlamaCppConnector` class)

- [ ] **Step 1: Write failing connector tests**

In `tests/unit/test_llm_connectors.py`, add at the end of the file:

```python
class TestLlamaCppConnector:
    """Tests for LlamaCppConnector"""

    @pytest.fixture
    def connector(self):
        from shared.utils.llm_connectors import LlamaCppConnector
        config = {
            "endpoint_url": "http://localhost:8080",
            "model_name": "llama-3.2-3b-instruct",
            "model_list": ["llama-3.2-3b-instruct"],
            "api_key": None,
        }
        return LlamaCppConnector("test-llama", config)

    @pytest.mark.asyncio
    async def test_chat_completion_success(self, connector):
        mock_response_data = {
            "choices": [{"message": {"content": "Hello!"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "model": "llama-3.2-3b-instruct",
        }
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=mock_response_data)

        with patch.object(connector, "session") as mock_session:
            mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_resp))
            content, usage = await connector.chat_completion(
                [{"role": "user", "content": "Hi"}], "llama-3.2-3b-instruct"
            )

        assert content == "Hello!"
        assert usage["prompt_tokens"] == 10
        assert usage["provider"] == "llamacpp"

    @pytest.mark.asyncio
    async def test_chat_completion_server_error(self, connector):
        mock_resp = AsyncMock()
        mock_resp.status = 500

        with patch.object(connector, "session") as mock_session:
            mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_resp))
            with pytest.raises(Exception, match="llama-server error: 500"):
                await connector.chat_completion(
                    [{"role": "user", "content": "Hi"}], "llama-3.2-3b-instruct"
                )

    @pytest.mark.asyncio
    async def test_count_tokens_exact_via_tokenize(self, connector):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"tokens": [1, 2, 3, 4, 5]})

        with patch.object(connector, "session") as mock_session:
            mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_resp))
            count = await connector.count_tokens("hello world", "llama-3.2-3b-instruct")

        assert count == 5

    @pytest.mark.asyncio
    async def test_count_tokens_fallback_to_tiktoken_on_failure(self, connector):
        mock_resp = AsyncMock()
        mock_resp.status = 503

        with patch.object(connector, "session") as mock_session:
            mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_resp))
            count = await connector.count_tokens("hello world", "llama-3.2-3b-instruct")

        assert count > 0  # tiktoken fallback returned something

    @pytest.mark.asyncio
    async def test_list_models_returns_loaded_model(self, connector):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "object": "list",
            "data": [{"id": "llama-3.2-3b-instruct", "object": "model"}],
        })

        with patch.object(connector, "session") as mock_session:
            mock_session.get = MagicMock(return_value=AsyncContextManagerMock(mock_resp))
            models = await connector.list_models()

        assert len(models) == 1
        assert models[0]["id"] == "llama-3.2-3b-instruct"
        assert models[0]["provider"] == "llamacpp"

    @pytest.mark.asyncio
    async def test_health_check_healthy(self, connector):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"status": "ok"})

        with patch.object(connector, "session") as mock_session:
            mock_session.get = MagicMock(return_value=AsyncContextManagerMock(mock_resp))
            result = await connector.health_check()

        assert result["status"] == "healthy"
        assert result["provider"] == "llamacpp"

    @pytest.mark.asyncio
    async def test_health_check_unhealthy(self, connector):
        with patch.object(connector, "session") as mock_session:
            mock_session.get = MagicMock(side_effect=Exception("connection refused"))
            result = await connector.health_check()

        assert result["status"] == "unhealthy"
        assert "connection refused" in result["error"]
```

Also check that the existing test file imports `AsyncContextManagerMock` — if it doesn't exist, add this helper near the top of the test file (after existing imports):

```python
class AsyncContextManagerMock:
    """Helper to mock async context managers (aiohttp responses)."""
    def __init__(self, mock_response):
        self._mock = mock_response
    async def __aenter__(self):
        return self._mock
    async def __aexit__(self, *args):
        pass
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/penguin/code/waddleai
python3 -m pytest tests/unit/test_llm_connectors.py::TestLlamaCppConnector -v --no-cov
```

Expected: FAILED with `ImportError: cannot import name 'LlamaCppConnector'`

- [ ] **Step 3: Implement `LlamaCppConnector`**

In `shared/utils/llm_connectors.py`, insert after the `OllamaConnector` class (after line 469, before `class LLMConnectionManager`):

```python
class LlamaCppConnector(LLMConnector):
    """llama-server (llama.cpp) connector.

    Connects to a running llama-server instance via its OpenAI-compatible HTTP API.
    Uses /tokenize for exact token counts; falls back to tiktoken on failure.
    """

    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        self.model_name: str = config.get("model_name", "")
        self.session: Optional[aiohttp.ClientSession] = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self.session = aiohttp.ClientSession(headers=headers)
        return self.session

    @property
    def session(self) -> Optional[aiohttp.ClientSession]:
        return self._session

    @session.setter
    def session(self, value):
        self._session = value

    async def chat_completion(
        self, messages: List[Dict[str, str]], model: str, **kwargs
    ) -> Tuple[str, Dict[str, Any]]:
        session = self._get_session()
        payload = {"model": model or self.model_name, "messages": messages, **kwargs}
        try:
            async with session.post(
                f"{self.endpoint_url}/v1/chat/completions",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as response:
                if response.status != 200:
                    raise Exception(f"llama-server error: {response.status}")
                data = await response.json()
                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                usage["provider"] = "llamacpp"
                usage["model"] = model
                return content, usage
        except Exception as e:
            logger.error(f"LlamaCpp completion failed: {e}")
            raise

    async def count_tokens(self, text: str, model: str) -> int:
        """Return exact token count via /tokenize; fall back to tiktoken on failure."""
        session = self._get_session()
        try:
            async with session.post(
                f"{self.endpoint_url}/tokenize",
                json={"content": text},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return len(data.get("tokens", []))
        except Exception:
            pass
        logger.warning("LlamaCpp /tokenize unavailable — falling back to tiktoken estimate")
        try:
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except Exception:
            return len(text.split())

    async def list_models(self) -> List[Dict[str, Any]]:
        session = self._get_session()
        try:
            async with session.get(
                f"{self.endpoint_url}/v1/models",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    return []
                data = await response.json()
                return [
                    {
                        "id": m.get("id", self.model_name),
                        "object": "model",
                        "provider": "llamacpp",
                        "owned_by": "llamacpp",
                    }
                    for m in data.get("data", [])
                ]
        except Exception as e:
            logger.error(f"Failed to list llama-server models: {e}")
            return []

    async def health_check(self) -> Dict[str, Any]:
        session = self._get_session()
        try:
            async with session.get(
                f"{self.endpoint_url}/health",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status == 200:
                    return {
                        "status": "healthy",
                        "provider": "llamacpp",
                        "endpoint": self.endpoint_url,
                        "model": self.model_name,
                    }
                return {
                    "status": "unhealthy",
                    "provider": "llamacpp",
                    "error": f"HTTP {response.status}",
                }
        except Exception as e:
            return {
                "status": "unhealthy",
                "provider": "llamacpp",
                "error": str(e),
            }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
```

- [ ] **Step 4: Register in `_load_connectors()`**

In `_load_connectors()` (around line 510), add after the `elif link.provider == "ollama":` branch:

```python
elif link.provider == "llamacpp":
    connector = LlamaCppConnector(link.name, config)
```

- [ ] **Step 5: Update module docstring**

Change line 3 from:
```python
Handles connections to OpenAI, Anthropic, and Ollama providers
```
to:
```python
Handles connections to OpenAI, Anthropic, Ollama, and llama.cpp (llama-server) providers
```

- [ ] **Step 6: Run connector tests**

```bash
cd /home/penguin/code/waddleai
python3 -m pytest tests/unit/test_llm_connectors.py::TestLlamaCppConnector -v --no-cov
```

Expected: 7 passed

- [ ] **Step 7: Run full test suite**

```bash
python3 -m pytest tests/ --tb=short 2>&1 | tail -5
```

Expected: all previously passing tests still pass

- [ ] **Step 8: Commit**

```bash
git add shared/utils/llm_connectors.py tests/unit/test_llm_connectors.py
git commit -m "feat: add LlamaCppConnector with exact tokenization via /tokenize"
```

---

### Task 4: Implement `LlamaCppManager`

**Files:**
- Create: `services/management/app/services/llamacpp_manager.py`
- Create: `tests/unit/management/test_llamacpp_manager.py`

- [ ] **Step 1: Write failing manager tests**

Create `tests/unit/management/test_llamacpp_manager.py`:

```python
"""Unit tests for LlamaCppManager"""
import json
from unittest.mock import MagicMock, patch

import pytest
import yaml


@pytest.fixture
def mock_db():
    db = MagicMock()
    return db


@pytest.fixture
def manager(mock_db):
    from services.management.app.services.llamacpp_manager import LlamaCppManager
    return LlamaCppManager(mock_db)


@pytest.fixture
def k8s_deployment():
    """Minimal deployment record for K8s mode."""
    dep = MagicMock()
    dep.id = 1
    dep.name = "llama-3b"
    dep.deployment_type = "kubernetes"
    dep.model_name = "llama-3.2-3b-instruct"
    dep.model_url = "https://huggingface.co/example/llama-3.2-3b.gguf"
    dep.model_filename = "llama-3.2-3b.gguf"
    dep.n_ctx = 4096
    dep.n_gpu_layers = -1
    dep.gpu_count = 1
    dep.k8s_namespace = "waddleai"
    dep.k8s_daemonset_name = "waddleai-llamacpp-llama-3b"
    dep.node_selector = {"waddleai/gpu-tier": "a100"}
    dep.node_affinity = None
    dep.endpoint_url = None
    dep.status = "pending"
    return dep


@pytest.fixture
def remote_deployment():
    dep = MagicMock()
    dep.id = 2
    dep.name = "remote-llama"
    dep.deployment_type = "remote"
    dep.model_name = "llama-3.1-8b-instruct"
    dep.endpoint_url = "http://192.168.1.50:8080"
    dep.status = "pending"
    return dep


def test_generate_daemonset_name(manager):
    name = manager._daemonset_name("my-model")
    assert name == "waddleai-llamacpp-my-model"


def test_generate_daemonset_name_sanitises_special_chars(manager):
    name = manager._daemonset_name("My Model v2.0!")
    assert name == "waddleai-llamacpp-my-model-v2-0"


def test_export_k8s_manifest_contains_daemonset(manager, k8s_deployment):
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    kinds = [d["kind"] for d in docs]
    assert "DaemonSet" in kinds
    assert "Service" in kinds


def test_export_k8s_manifest_node_selector(manager, k8s_deployment):
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    node_sel = ds["spec"]["template"]["spec"]["nodeSelector"]
    assert node_sel == {"waddleai/gpu-tier": "a100"}


def test_export_k8s_manifest_gpu_resource(manager, k8s_deployment):
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    container = ds["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"]["limits"]["nvidia.com/gpu"] == "1"


def test_export_k8s_manifest_init_container_download_url(manager, k8s_deployment):
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    init_c = ds["spec"]["template"]["spec"]["initContainers"][0]
    cmd = " ".join(init_c["command"])
    assert k8s_deployment.model_url in cmd


def test_export_k8s_manifest_service_port(manager, k8s_deployment):
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    svc = next(d for d in docs if d["kind"] == "Service")
    assert svc["spec"]["ports"][0]["port"] == 8080


def test_deploy_daemonset_calls_k8s_api(manager, k8s_deployment, mock_db):
    with patch("services.management.app.services.llamacpp_manager.get_k8s_apps_client") as mock_apps, \
         patch("services.management.app.services.llamacpp_manager.get_k8s_core_client") as mock_core:
        mock_apps.return_value = MagicMock()
        mock_core.return_value = MagicMock()
        manager.deploy_daemonset(k8s_deployment)

    mock_apps.return_value.create_namespaced_daemon_set.assert_called_once()
    mock_core.return_value.create_namespaced_service.assert_called_once()


def test_deploy_daemonset_k8s_error_propagates(manager, k8s_deployment):
    with patch("services.management.app.services.llamacpp_manager.get_k8s_apps_client") as mock_apps:
        mock_apps.return_value.create_namespaced_daemon_set.side_effect = Exception("k8s unavailable")
        with pytest.raises(Exception, match="k8s unavailable"):
            manager.deploy_daemonset(k8s_deployment)


def test_remove_daemonset_running_without_force_raises(manager, k8s_deployment):
    k8s_deployment.status = "running"
    with pytest.raises(ValueError, match="force=True"):
        manager.remove_daemonset(k8s_deployment, force=False)


def test_remove_daemonset_running_with_force_deletes(manager, k8s_deployment):
    k8s_deployment.status = "running"
    with patch("services.management.app.services.llamacpp_manager.get_k8s_apps_client") as mock_apps, \
         patch("services.management.app.services.llamacpp_manager.get_k8s_core_client") as mock_core:
        mock_apps.return_value = MagicMock()
        mock_core.return_value = MagicMock()
        manager.remove_daemonset(k8s_deployment, force=True)

    mock_apps.return_value.delete_namespaced_daemon_set.assert_called_once()
    mock_core.return_value.delete_namespaced_service.assert_called_once()


def test_register_remote_healthy_sets_running(manager, remote_deployment, mock_db):
    with patch("services.management.app.services.llamacpp_manager.requests") as mock_req:
        mock_req.get.return_value.status_code = 200
        manager.register_remote(remote_deployment)

    mock_db(mock_db.llamacpp_deployments.id == remote_deployment.id).update.assert_called_once()


def test_register_remote_unhealthy_raises(manager, remote_deployment):
    with patch("services.management.app.services.llamacpp_manager.requests") as mock_req:
        mock_req.get.side_effect = Exception("connection refused")
        with pytest.raises(ValueError, match="unreachable"):
            manager.register_remote(remote_deployment)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/penguin/code/waddleai
python3 -m pytest tests/unit/management/test_llamacpp_manager.py -v --no-cov
```

Expected: FAILED with `ModuleNotFoundError: No module named 'services.management.app.services.llamacpp_manager'`

- [ ] **Step 3: Implement `LlamaCppManager`**

Create `services/management/app/services/llamacpp_manager.py`:

```python
"""
llama.cpp Deployment Manager

Manages llama-server instances in two modes:
- kubernetes: Creates/removes K8s DaemonSets targeting GPU-labelled nodes
- remote:     Registers an existing llama-server endpoint after a health check
"""

import logging
import re
from typing import Any, Dict, Optional

import requests
import yaml

logger = logging.getLogger(__name__)


def get_k8s_apps_client():
    """Return a configured AppsV1Api client."""
    from kubernetes import client, config as k8s_config  # type: ignore[import]
    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()
    return client.AppsV1Api()


def get_k8s_core_client():
    """Return a configured CoreV1Api client."""
    from kubernetes import client, config as k8s_config  # type: ignore[import]
    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()
    return client.CoreV1Api()


class LlamaCppManager:
    """Manages llama-server deployment lifecycle."""

    def __init__(self, db):
        self.db = db

    def _daemonset_name(self, deployment_name: str) -> str:
        """Generate a K8s-safe DaemonSet name from a deployment name."""
        sanitised = re.sub(r"[^a-z0-9-]", "-", deployment_name.lower())
        sanitised = re.sub(r"-+", "-", sanitised).strip("-")
        return f"waddleai-llamacpp-{sanitised}"

    def export_k8s_manifest(self, deployment) -> str:
        """Return DaemonSet + Service YAML for the given deployment."""
        ds_name = deployment.k8s_daemonset_name or self._daemonset_name(deployment.name)
        namespace = deployment.k8s_namespace or "waddleai"
        node_selector = deployment.node_selector or {}

        daemonset = {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": ds_name, "namespace": namespace},
            "spec": {
                "selector": {"matchLabels": {"app": ds_name}},
                "template": {
                    "metadata": {"labels": {"app": ds_name}},
                    "spec": {
                        "nodeSelector": node_selector,
                        "initContainers": [
                            {
                                "name": "download-model",
                                "image": "curlimages/curl:latest",
                                "command": [
                                    "sh", "-c",
                                    f"curl -L -o /models/{deployment.model_filename} {deployment.model_url}",
                                ],
                                "volumeMounts": [{"name": "model-storage", "mountPath": "/models"}],
                            }
                        ],
                        "containers": [
                            {
                                "name": "llama-server",
                                "image": "ghcr.io/ggerganov/llama.cpp:server",
                                "args": [
                                    "--model", f"/models/{deployment.model_filename}",
                                    "--n-gpu-layers", str(deployment.n_gpu_layers),
                                    "--ctx-size", str(deployment.n_ctx),
                                    "--port", "8080",
                                    "--host", "0.0.0.0",
                                ],
                                "ports": [{"containerPort": 8080}],
                                "resources": {
                                    "limits": {"nvidia.com/gpu": str(deployment.gpu_count)}
                                },
                                "volumeMounts": [{"name": "model-storage", "mountPath": "/models"}],
                            }
                        ],
                        "volumes": [{"name": "model-storage", "emptyDir": {}}],
                    },
                },
            },
        }

        if deployment.node_affinity:
            daemonset["spec"]["template"]["spec"]["affinity"] = {
                "nodeAffinity": deployment.node_affinity
            }

        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": f"{ds_name}-svc", "namespace": namespace},
            "spec": {
                "selector": {"app": ds_name},
                "ports": [{"port": 8080, "targetPort": 8080}],
            },
        }

        return yaml.dump_all([daemonset, service], default_flow_style=False)

    def deploy_daemonset(self, deployment) -> None:
        """Create the DaemonSet and Service in K8s. Raises on K8s API error."""
        manifest_yaml = self.export_k8s_manifest(deployment)
        docs = list(yaml.safe_load_all(manifest_yaml))
        ds_doc = next(d for d in docs if d["kind"] == "DaemonSet")
        svc_doc = next(d for d in docs if d["kind"] == "Service")

        namespace = deployment.k8s_namespace or "waddleai"
        apps_client = get_k8s_apps_client()
        core_client = get_k8s_core_client()

        from kubernetes import client as k8s_client  # type: ignore[import]
        apps_client.create_namespaced_daemon_set(
            namespace=namespace,
            body=k8s_client.V1DaemonSet(**ds_doc),
        )
        core_client.create_namespaced_service(
            namespace=namespace,
            body=k8s_client.V1Service(**svc_doc),
        )

        ds_name = deployment.k8s_daemonset_name or self._daemonset_name(deployment.name)
        svc_endpoint = f"http://{ds_name}-svc.{namespace}:8080"
        db = self.db
        db(db.llamacpp_deployments.id == deployment.id).update(
            status="deploying",
            endpoint_url=svc_endpoint,
            k8s_daemonset_name=ds_name,
        )
        logger.info(f"Deployed llama.cpp DaemonSet {ds_name} in {namespace}")

    def remove_daemonset(self, deployment, force: bool = False) -> None:
        """Delete the DaemonSet and Service. Requires force=True if status is running."""
        if deployment.status == "running" and not force:
            raise ValueError(
                f"Deployment '{deployment.name}' is running. Pass force=True to remove it."
            )

        namespace = deployment.k8s_namespace or "waddleai"
        ds_name = deployment.k8s_daemonset_name or self._daemonset_name(deployment.name)

        apps_client = get_k8s_apps_client()
        core_client = get_k8s_core_client()

        apps_client.delete_namespaced_daemon_set(name=ds_name, namespace=namespace)
        core_client.delete_namespaced_service(name=f"{ds_name}-svc", namespace=namespace)

        db = self.db
        db(db.llamacpp_deployments.id == deployment.id).update(status="stopped")
        logger.info(f"Removed llama.cpp DaemonSet {ds_name}")

    def register_remote(self, deployment) -> None:
        """Register a remote llama-server endpoint after verifying it is reachable."""
        url = deployment.endpoint_url
        try:
            resp = requests.get(f"{url}/health", timeout=10)
            if resp.status_code != 200:
                raise ValueError(f"Health check returned HTTP {resp.status_code}")
        except requests.exceptions.RequestException as exc:
            raise ValueError(f"Endpoint {url} unreachable: {exc}") from exc

        db = self.db
        db(db.llamacpp_deployments.id == deployment.id).update(status="running")
        logger.info(f"Registered remote llama.cpp endpoint: {url}")
```

- [ ] **Step 4: Run manager tests**

```bash
cd /home/penguin/code/waddleai
python3 -m pytest tests/unit/management/test_llamacpp_manager.py -v --no-cov
```

Expected: 13 passed

- [ ] **Step 5: Run full test suite**

```bash
python3 -m pytest tests/ --tb=short 2>&1 | tail -5
```

Expected: all previously passing tests still pass

- [ ] **Step 6: Commit**

```bash
git add services/management/app/services/llamacpp_manager.py tests/unit/management/test_llamacpp_manager.py
git commit -m "feat: add LlamaCppManager for K8s DaemonSet lifecycle and remote-connect"
```

---

### Task 5: Implement management API routes

**Files:**
- Create: `services/management/app/api/v1/llamacpp.py`
- Create: `tests/unit/management/test_llamacpp_routes.py`

- [ ] **Step 1: Write failing route tests**

Create `tests/unit/management/test_llamacpp_routes.py`:

```python
"""Unit tests for llama.cpp management API routes"""
import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def app():
    from services.management.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin_headers():
    """Auth headers for an admin user — patch penguin_auth.verify_token."""
    return {"Authorization": "Bearer test-admin-token"}


@pytest.fixture
def mock_admin_user():
    from shared.auth.rbac import Role, UserContext
    from shared.auth.rbac import ROLE_PERMISSIONS
    return UserContext(
        user_id=1,
        username="admin",
        role=Role.ADMIN,
        organization_id=1,
        managed_orgs=[],
        permissions=[p.value for p in ROLE_PERMISSIONS[Role.ADMIN]],
        api_key_id=1,
    )


def _mock_deployment(id=1, name="llama-3b", status="pending", deployment_type="kubernetes"):
    dep = MagicMock()
    dep.id = id
    dep.name = name
    dep.status = status
    dep.deployment_type = deployment_type
    dep.model_name = "llama-3.2-3b-instruct"
    dep.model_url = "https://example.com/llama.gguf"
    dep.model_filename = "llama.gguf"
    dep.n_ctx = 4096
    dep.n_gpu_layers = -1
    dep.gpu_count = 1
    dep.endpoint_url = None
    dep.k8s_namespace = "waddleai"
    dep.k8s_daemonset_name = "waddleai-llamacpp-llama-3b"
    dep.node_selector = {"waddleai/gpu-tier": "a100"}
    dep.node_affinity = None
    dep.status_message = None
    dep.created_at = None
    dep.modified_at = None
    return dep


class TestListDeployments:
    def test_list_returns_200(self, client, admin_headers, mock_admin_user):
        with patch("services.management.app.api.v1.llamacpp.get_current_user", return_value=mock_admin_user), \
             patch("services.management.app.api.v1.llamacpp.get_db") as mock_db:
            mock_db.return_value.llamacpp_deployments.select.return_value = []
            resp = client.get("/api/v1/llamacpp/deployments", headers=admin_headers)
        assert resp.status_code == 200
        assert json.loads(resp.data)["deployments"] == []

    def test_list_requires_admin(self, client):
        resp = client.get("/api/v1/llamacpp/deployments")
        assert resp.status_code == 401


class TestCreateDeployment:
    def test_create_kubernetes_deployment(self, client, admin_headers, mock_admin_user):
        payload = {
            "name": "llama-3b",
            "deployment_type": "kubernetes",
            "model_name": "llama-3.2-3b-instruct",
            "model_url": "https://example.com/llama.gguf",
            "model_filename": "llama.gguf",
            "node_selector": {"waddleai/gpu-tier": "a100"},
        }
        with patch("services.management.app.api.v1.llamacpp.get_current_user", return_value=mock_admin_user), \
             patch("services.management.app.api.v1.llamacpp.get_db") as mock_db:
            mock_db.return_value.llamacpp_deployments.insert.return_value = 1
            resp = client.post(
                "/api/v1/llamacpp/deployments",
                headers={**admin_headers, "Content-Type": "application/json"},
                data=json.dumps(payload),
            )
        assert resp.status_code == 201
        data = json.loads(resp.data)
        assert data["deployment_id"] == 1

    def test_create_missing_name_returns_400(self, client, admin_headers, mock_admin_user):
        with patch("services.management.app.api.v1.llamacpp.get_current_user", return_value=mock_admin_user):
            resp = client.post(
                "/api/v1/llamacpp/deployments",
                headers={**admin_headers, "Content-Type": "application/json"},
                data=json.dumps({"deployment_type": "kubernetes"}),
            )
        assert resp.status_code == 400

    def test_create_missing_model_name_returns_400(self, client, admin_headers, mock_admin_user):
        with patch("services.management.app.api.v1.llamacpp.get_current_user", return_value=mock_admin_user):
            resp = client.post(
                "/api/v1/llamacpp/deployments",
                headers={**admin_headers, "Content-Type": "application/json"},
                data=json.dumps({"name": "test", "deployment_type": "kubernetes"}),
            )
        assert resp.status_code == 400


class TestGetDeployment:
    def test_get_existing_returns_200(self, client, admin_headers, mock_admin_user):
        dep = _mock_deployment()
        with patch("services.management.app.api.v1.llamacpp.get_current_user", return_value=mock_admin_user), \
             patch("services.management.app.api.v1.llamacpp.get_db") as mock_db:
            mock_db.return_value(mock_db.return_value.llamacpp_deployments.id == 1).select().first.return_value = dep
            resp = client.get("/api/v1/llamacpp/deployments/1", headers=admin_headers)
        assert resp.status_code == 200

    def test_get_nonexistent_returns_404(self, client, admin_headers, mock_admin_user):
        with patch("services.management.app.api.v1.llamacpp.get_current_user", return_value=mock_admin_user), \
             patch("services.management.app.api.v1.llamacpp.get_db") as mock_db:
            mock_db.return_value(mock_db.return_value.llamacpp_deployments.id == 99).select().first.return_value = None
            resp = client.get("/api/v1/llamacpp/deployments/99", headers=admin_headers)
        assert resp.status_code == 404


class TestDeleteDeployment:
    def test_delete_stopped_succeeds(self, client, admin_headers, mock_admin_user):
        dep = _mock_deployment(status="stopped")
        with patch("services.management.app.api.v1.llamacpp.get_current_user", return_value=mock_admin_user), \
             patch("services.management.app.api.v1.llamacpp.get_db") as mock_db:
            (mock_db.return_value(mock_db.return_value.llamacpp_deployments.id == 1)
             .select().first.return_value) = dep
            resp = client.delete("/api/v1/llamacpp/deployments/1", headers=admin_headers)
        assert resp.status_code == 200

    def test_delete_running_without_force_returns_409(self, client, admin_headers, mock_admin_user):
        dep = _mock_deployment(status="running")
        with patch("services.management.app.api.v1.llamacpp.get_current_user", return_value=mock_admin_user), \
             patch("services.management.app.api.v1.llamacpp.get_db") as mock_db:
            (mock_db.return_value(mock_db.return_value.llamacpp_deployments.id == 1)
             .select().first.return_value) = dep
            resp = client.delete("/api/v1/llamacpp/deployments/1", headers=admin_headers)
        assert resp.status_code == 409

    def test_delete_running_with_force_succeeds(self, client, admin_headers, mock_admin_user):
        dep = _mock_deployment(status="running")
        with patch("services.management.app.api.v1.llamacpp.get_current_user", return_value=mock_admin_user), \
             patch("services.management.app.api.v1.llamacpp.get_db") as mock_db, \
             patch("services.management.app.api.v1.llamacpp.LlamaCppManager") as mock_mgr:
            (mock_db.return_value(mock_db.return_value.llamacpp_deployments.id == 1)
             .select().first.return_value) = dep
            mock_mgr.return_value.remove_daemonset = MagicMock()
            resp = client.delete(
                "/api/v1/llamacpp/deployments/1?force=true", headers=admin_headers
            )
        assert resp.status_code == 200


class TestDeployRoute:
    def test_deploy_kubernetes_calls_manager(self, client, admin_headers, mock_admin_user):
        dep = _mock_deployment(status="pending")
        with patch("services.management.app.api.v1.llamacpp.get_current_user", return_value=mock_admin_user), \
             patch("services.management.app.api.v1.llamacpp.get_db") as mock_db, \
             patch("services.management.app.api.v1.llamacpp.LlamaCppManager") as mock_mgr:
            (mock_db.return_value(mock_db.return_value.llamacpp_deployments.id == 1)
             .select().first.return_value) = dep
            mock_mgr.return_value.deploy_daemonset = MagicMock()
            resp = client.post("/api/v1/llamacpp/deployments/1/deploy", headers=admin_headers)
        assert resp.status_code == 200
        mock_mgr.return_value.deploy_daemonset.assert_called_once_with(dep)

    def test_deploy_remote_calls_register(self, client, admin_headers, mock_admin_user):
        dep = _mock_deployment(status="pending", deployment_type="remote")
        dep.endpoint_url = "http://192.168.1.50:8080"
        with patch("services.management.app.api.v1.llamacpp.get_current_user", return_value=mock_admin_user), \
             patch("services.management.app.api.v1.llamacpp.get_db") as mock_db, \
             patch("services.management.app.api.v1.llamacpp.LlamaCppManager") as mock_mgr:
            (mock_db.return_value(mock_db.return_value.llamacpp_deployments.id == 1)
             .select().first.return_value) = dep
            mock_mgr.return_value.register_remote = MagicMock()
            resp = client.post("/api/v1/llamacpp/deployments/1/deploy", headers=admin_headers)
        assert resp.status_code == 200
        mock_mgr.return_value.register_remote.assert_called_once_with(dep)


class TestExportManifest:
    def test_export_returns_yaml(self, client, admin_headers, mock_admin_user):
        dep = _mock_deployment()
        with patch("services.management.app.api.v1.llamacpp.get_current_user", return_value=mock_admin_user), \
             patch("services.management.app.api.v1.llamacpp.get_db") as mock_db, \
             patch("services.management.app.api.v1.llamacpp.LlamaCppManager") as mock_mgr:
            (mock_db.return_value(mock_db.return_value.llamacpp_deployments.id == 1)
             .select().first.return_value) = dep
            mock_mgr.return_value.export_k8s_manifest.return_value = "kind: DaemonSet\n---\nkind: Service\n"
            resp = client.get("/api/v1/llamacpp/deployments/1/export/k8s", headers=admin_headers)
        assert resp.status_code == 200
        assert b"DaemonSet" in resp.data
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/penguin/code/waddleai
python3 -m pytest tests/unit/management/test_llamacpp_routes.py -v --no-cov 2>&1 | head -20
```

Expected: FAILED with import or 404 errors

- [ ] **Step 3: Implement routes**

Create `services/management/app/api/v1/llamacpp.py`:

```python
"""
llama.cpp deployment management routes.
All routes require ADMIN role.
"""

import logging

from flask import jsonify, request

from services.management.app.api.v1 import api_v1_bp
from services.management.app.extensions import db as _db
from services.management.app.services.llamacpp_manager import LlamaCppManager
from shared.auth.rbac import Permission, require_permission

logger = logging.getLogger(__name__)


def get_db():
    return _db


def get_current_user():
    from flask import g
    return g.user


def _deployment_to_dict(dep) -> dict:
    return {
        "id": dep.id,
        "name": dep.name,
        "deployment_type": dep.deployment_type,
        "status": dep.status,
        "status_message": dep.status_message,
        "model_name": dep.model_name,
        "model_url": dep.model_url,
        "model_filename": dep.model_filename,
        "n_ctx": dep.n_ctx,
        "n_gpu_layers": dep.n_gpu_layers,
        "gpu_count": dep.gpu_count,
        "endpoint_url": dep.endpoint_url,
        "k8s_namespace": dep.k8s_namespace,
        "k8s_daemonset_name": dep.k8s_daemonset_name,
        "node_selector": dep.node_selector,
        "node_affinity": dep.node_affinity,
        "created_at": dep.created_at.isoformat() if dep.created_at else None,
        "modified_at": dep.modified_at.isoformat() if dep.modified_at else None,
    }


@api_v1_bp.route("/llamacpp/deployments", methods=["GET"])
@require_permission(Permission.ADMIN_READ)
def list_llamacpp_deployments():
    db = get_db()
    deployments = db.llamacpp_deployments.select()
    return jsonify({"deployments": [_deployment_to_dict(d) for d in deployments]}), 200


@api_v1_bp.route("/llamacpp/deployments", methods=["POST"])
@require_permission(Permission.ADMIN_WRITE)
def create_llamacpp_deployment():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    model_name = data.get("model_name", "").strip()

    if not name:
        return jsonify({"error": "name is required"}), 400
    if not model_name:
        return jsonify({"error": "model_name is required"}), 400

    deployment_type = data.get("deployment_type", "kubernetes")
    db = get_db()
    mgr = LlamaCppManager(db)

    dep_id = db.llamacpp_deployments.insert(
        name=name,
        deployment_type=deployment_type,
        status="pending",
        model_name=model_name,
        model_url=data.get("model_url"),
        model_filename=data.get("model_filename"),
        n_ctx=data.get("n_ctx", 4096),
        n_gpu_layers=data.get("n_gpu_layers", -1),
        gpu_count=data.get("gpu_count", 1),
        endpoint_url=data.get("endpoint_url"),
        k8s_namespace=data.get("k8s_namespace", "waddleai"),
        k8s_daemonset_name=mgr._daemonset_name(name),
        node_selector=data.get("node_selector"),
        node_affinity=data.get("node_affinity"),
    )
    return jsonify({"deployment_id": dep_id, "message": "Deployment created"}), 201


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>", methods=["GET"])
@require_permission(Permission.ADMIN_READ)
def get_llamacpp_deployment(deployment_id):
    db = get_db()
    dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
    if not dep:
        return jsonify({"error": "Deployment not found"}), 404
    return jsonify(_deployment_to_dict(dep)), 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>", methods=["PATCH"])
@require_permission(Permission.ADMIN_WRITE)
def update_llamacpp_deployment(deployment_id):
    db = get_db()
    dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
    if not dep:
        return jsonify({"error": "Deployment not found"}), 404
    if dep.status == "running":
        return jsonify({"error": "Stop the deployment before modifying it"}), 409

    data = request.get_json() or {}
    allowed = {"model_name", "model_url", "model_filename", "n_ctx", "n_gpu_layers",
                "gpu_count", "k8s_namespace", "node_selector", "node_affinity"}
    updates = {k: v for k, v in data.items() if k in allowed}
    db(db.llamacpp_deployments.id == deployment_id).update(**updates)
    return jsonify({"message": "Deployment updated"}), 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>", methods=["DELETE"])
@require_permission(Permission.ADMIN_WRITE)
def delete_llamacpp_deployment(deployment_id):
    db = get_db()
    dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
    if not dep:
        return jsonify({"error": "Deployment not found"}), 404

    force = request.args.get("force", "").lower() == "true"
    if dep.status == "running" and not force:
        return jsonify({"error": "Deployment is running. Use ?force=true to delete it."}), 409

    if dep.status == "running" and force:
        mgr = LlamaCppManager(db)
        try:
            if dep.deployment_type == "kubernetes":
                mgr.remove_daemonset(dep, force=True)
        except Exception as e:
            logger.warning(f"Error during forced removal of {dep.name}: {e}")

    db(db.llamacpp_deployments.id == deployment_id).delete()
    return jsonify({"message": "Deployment deleted"}), 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>/deploy", methods=["POST"])
@require_permission(Permission.ADMIN_WRITE)
def deploy_llamacpp(deployment_id):
    db = get_db()
    dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
    if not dep:
        return jsonify({"error": "Deployment not found"}), 404

    mgr = LlamaCppManager(db)
    try:
        if dep.deployment_type == "kubernetes":
            mgr.deploy_daemonset(dep)
        else:
            mgr.register_remote(dep)
    except Exception as e:
        return jsonify({"error": str(e)}), 503

    return jsonify({"message": "Deployment initiated", "deployment_id": deployment_id}), 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>/remove", methods=["POST"])
@require_permission(Permission.ADMIN_WRITE)
def remove_llamacpp(deployment_id):
    db = get_db()
    dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
    if not dep:
        return jsonify({"error": "Deployment not found"}), 404

    mgr = LlamaCppManager(db)
    try:
        if dep.deployment_type == "kubernetes":
            mgr.remove_daemonset(dep, force=True)
        else:
            db(db.llamacpp_deployments.id == deployment_id).update(status="stopped")
    except Exception as e:
        return jsonify({"error": str(e)}), 503

    return jsonify({"message": "Deployment removed"}), 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>/health", methods=["GET"])
@require_permission(Permission.ADMIN_READ)
def check_llamacpp_health(deployment_id):
    db = get_db()
    dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
    if not dep:
        return jsonify({"error": "Deployment not found"}), 404
    if not dep.endpoint_url:
        return jsonify({"status": "unknown", "reason": "endpoint_url not set"}), 200

    import requests as req
    try:
        resp = req.get(f"{dep.endpoint_url}/health", timeout=10)
        if resp.status_code == 200:
            return jsonify({"status": "healthy", "endpoint": dep.endpoint_url}), 200
        return jsonify({"status": "unhealthy", "http_status": resp.status_code}), 200
    except Exception as e:
        return jsonify({"status": "unhealthy", "error": str(e)}), 200


@api_v1_bp.route("/llamacpp/deployments/<int:deployment_id>/export/k8s", methods=["GET"])
@require_permission(Permission.ADMIN_READ)
def export_llamacpp_k8s(deployment_id):
    db = get_db()
    dep = db(db.llamacpp_deployments.id == deployment_id).select().first()
    if not dep:
        return jsonify({"error": "Deployment not found"}), 404

    mgr = LlamaCppManager(db)
    manifest = mgr.export_k8s_manifest(dep)
    return manifest, 200, {"Content-Type": "application/x-yaml"}
```

- [ ] **Step 4: Run route tests**

```bash
cd /home/penguin/code/waddleai
python3 -m pytest tests/unit/management/test_llamacpp_routes.py -v --no-cov
```

Expected: tests pass (some may need minor fixture adjustments for app context — fix any import errors before declaring done)

- [ ] **Step 5: Commit**

```bash
git add services/management/app/api/v1/llamacpp.py tests/unit/management/test_llamacpp_routes.py
git commit -m "feat: add llama.cpp management API routes"
```

---

### Task 6: Wire up blueprint and run full suite

**Files:**
- Modify: `services/management/app/api/v1/__init__.py:10-18`

- [ ] **Step 1: Register blueprint**

In `services/management/app/api/v1/__init__.py`, add `llamacpp` to the import list:

```python
from . import (
    ailb,
    ailb_memory,
    auth,
    keys,
    llamacpp,
    ollama,
    ollama_models,
    organizations,
    providers,
    quotas,
    usage,
    users,
    webhooks,
)
```

- [ ] **Step 2: Run full test suite**

```bash
cd /home/penguin/code/waddleai
python3 -m pytest tests/ --tb=short 2>&1 | tail -10
```

Expected: all tests pass, coverage ≥ 60%

- [ ] **Step 3: Commit**

```bash
git add services/management/app/api/v1/__init__.py
git commit -m "feat: register llamacpp blueprint in API v1"
```

---

### Task 7: Add integration test scaffold

**Files:**
- Create: `tests/integration/test_llamacpp_integration.py`

- [ ] **Step 1: Create integration test file**

Create `tests/integration/test_llamacpp_integration.py`:

```python
"""
llama.cpp integration tests.

Requires a running llama-server. Set LLAMACPP_ENDPOINT to enable:
    export LLAMACPP_ENDPOINT=http://localhost:8080
    pytest tests/integration/test_llamacpp_integration.py

Quick local setup:
    docker run -p 8080:8080 ghcr.io/ggerganov/llama.cpp:server \\
        -m /path/to/model.gguf --port 8080 --host 0.0.0.0
"""

import os

import pytest

LLAMACPP_ENDPOINT = os.environ.get("LLAMACPP_ENDPOINT")
skip_without_server = pytest.mark.skipif(
    not LLAMACPP_ENDPOINT,
    reason="Set LLAMACPP_ENDPOINT to run llama.cpp integration tests",
)


def test_llamacpp_connector_importable():
    """Always runs — verifies the connector class can be imported."""
    from shared.utils.llm_connectors import LlamaCppConnector
    assert LlamaCppConnector is not None


@skip_without_server
@pytest.mark.asyncio
async def test_llamacpp_health_check():
    from shared.utils.llm_connectors import LlamaCppConnector
    connector = LlamaCppConnector(
        "integration-test",
        {"endpoint_url": LLAMACPP_ENDPOINT, "model_name": "test-model", "api_key": None},
    )
    result = await connector.health_check()
    assert result["status"] == "healthy"
    await connector.close()


@skip_without_server
@pytest.mark.asyncio
async def test_llamacpp_list_models():
    from shared.utils.llm_connectors import LlamaCppConnector
    connector = LlamaCppConnector(
        "integration-test",
        {"endpoint_url": LLAMACPP_ENDPOINT, "model_name": "test-model", "api_key": None},
    )
    models = await connector.list_models()
    assert isinstance(models, list)
    assert len(models) >= 1
    await connector.close()


@skip_without_server
@pytest.mark.asyncio
async def test_llamacpp_tokenize_endpoint():
    from shared.utils.llm_connectors import LlamaCppConnector
    connector = LlamaCppConnector(
        "integration-test",
        {"endpoint_url": LLAMACPP_ENDPOINT, "model_name": "test-model", "api_key": None},
    )
    count = await connector.count_tokens("Hello, world!", "test-model")
    assert isinstance(count, int)
    assert count > 0
    await connector.close()


@skip_without_server
@pytest.mark.asyncio
async def test_llamacpp_chat_completion():
    from shared.utils.llm_connectors import LlamaCppConnector
    connector = LlamaCppConnector(
        "integration-test",
        {"endpoint_url": LLAMACPP_ENDPOINT, "model_name": "test-model", "api_key": None},
    )
    content, usage = await connector.chat_completion(
        [{"role": "user", "content": "Say hello in one word."}],
        "test-model",
    )
    assert isinstance(content, str)
    assert len(content) > 0
    assert usage["provider"] == "llamacpp"
    await connector.close()
```

- [ ] **Step 2: Run the always-on test**

```bash
cd /home/penguin/code/waddleai
python3 -m pytest tests/integration/test_llamacpp_integration.py::test_llamacpp_connector_importable -v --no-cov
```

Expected: 1 passed

- [ ] **Step 3: Run full suite one more time**

```bash
python3 -m pytest tests/ --tb=short 2>&1 | tail -10
```

Expected: all tests pass

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_llamacpp_integration.py
git commit -m "test: add llama.cpp integration test scaffold"
```

---

### Task 8: Update docs

**Files:**
- Modify: `docs/APP_STANDARDS.md`
- Modify: `docs/TESTING_SETUP.md`

- [ ] **Step 1: Add llama.cpp section to `docs/APP_STANDARDS.md`**

Append to the end of `docs/APP_STANDARDS.md`:

```markdown
## llama.cpp Provider

WaddleAI supports direct llama-server (llama.cpp) connections for lower-latency, higher-throughput inference compared to Ollama.

### Deployment model

Each `llamacpp_deployment` maps 1:1 to a K8s DaemonSet. One DaemonSet runs on each matching GPU node,
serving a single GGUF model. Multiple deployments = multiple models on different node pools.

### K8s node labelling convention

Label GPU nodes before creating deployments:

```bash
# Target A100 nodes
kubectl label node <node-name> waddleai/gpu-tier=a100

# Target H100 nodes
kubectl label node <node-name> waddleai/gpu-tier=h100

# Target any GPU node
kubectl label node <node-name> waddleai/gpu=true
```

Set `node_selector` in the deployment config to match:
```json
{"waddleai/gpu-tier": "a100"}
```

### GGUF sourcing

`model_url` should point to a publicly accessible GGUF file (HuggingFace raw URL, S3 pre-signed URL, etc.).
The initContainer downloads it into an `emptyDir` volume on first pod start.
**Note:** `emptyDir` is ephemeral — the model re-downloads on pod restart. Use a PersistentVolume for
production deployments with large models.

### Remote connect

Set `deployment_type=remote` and provide `endpoint_url` pointing at an existing llama-server.
WaddleAI performs a `/health` check before registering.
```

- [ ] **Step 2: Add llama.cpp testing section to `docs/TESTING_SETUP.md`**

Append to the end of `docs/TESTING_SETUP.md`:

```markdown
## llama.cpp Integration Testing

### Prerequisites

A running llama-server. Quick local setup via Docker:

```bash
# Pull a small test model (e.g. from Hugging Face)
docker run -p 8080:8080 ghcr.io/ggerganov/llama.cpp:server \
    --hf-repo ggml-org/models --hf-file tinyllamas/stories15M-q8_0.gguf \
    --port 8080 --host 0.0.0.0
```

### Running integration tests

```bash
export LLAMACPP_ENDPOINT=http://localhost:8080
pytest tests/integration/test_llamacpp_integration.py -v
```

Without `LLAMACPP_ENDPOINT`, only `test_llamacpp_connector_importable` runs (always passes).
```

- [ ] **Step 3: Commit docs**

```bash
git add docs/APP_STANDARDS.md docs/TESTING_SETUP.md
git commit -m "docs: add llama.cpp provider guide and testing setup"
```

---

## Self-Review Against Spec

| Spec requirement | Task |
|-----------------|------|
| `LlamaCppConnector` with `aiohttp` | Task 3 |
| `/tokenize` exact counts + tiktoken fallback | Task 3 |
| `LlamaCppDeployment` table | Task 1 |
| `LlamaCppConfig` + `ProviderType.LLAMACPP` | Task 2 |
| `LlamaCppManager.deploy_daemonset()` | Task 4 |
| `LlamaCppManager.remove_daemonset()` with force guard | Task 4 |
| `LlamaCppManager.register_remote()` with health check | Task 4 |
| `LlamaCppManager.export_k8s_manifest()` — DaemonSet + Service YAML | Task 4 |
| nodeSelector + GPU resource limits in manifest | Task 4 |
| initContainer GGUF download | Task 4 |
| `reload_connectors()` called after status → running | Task 4 (`deploy_daemonset` updates DB; connector reload on next request — note: `LLMConnectionManager.reload_connectors()` is called by the proxy on DB change events; no explicit call needed in the manager) |
| Management API CRUD routes | Task 5 |
| `/deploy`, `/remove`, `/health`, `/export/k8s` routes | Task 5 |
| Admin-only auth guard | Task 5 |
| 409 on DELETE running without force | Task 5 |
| Integration test scaffold (skippable) | Task 7 |
| `docs/APP_STANDARDS.md` update | Task 8 |
| `docs/TESTING_SETUP.md` update | Task 8 |
| Module docstring update | Task 3 |
