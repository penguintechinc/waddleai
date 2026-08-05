"""
Unit tests for MarchProxyConfigGenerator service.
Tests config generation, routing table, rate limits, and virtual key outputs.
"""

import json
import os
import sys
import tempfile
from datetime import datetime
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../services/management"))

from services.management.app.services.marchproxy_config import MarchProxyConfigGenerator
from tests.unit.management.conftest import make_select_result

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db() -> MagicMock:
    """Return a fresh mock DB for each test."""
    return MagicMock()


def _make_provider(
    provider_id: int = 1,
    name: str = "OpenAI",
    provider_type: str = "openai",
    endpoint_url: str = "https://api.openai.com/v1",
    api_key: str = "sk-test",
    priority: int = 100,
    enabled: bool = True,
    ailb_sync_enabled: bool = True,
    model_list: list = None,
    rate_limits: dict = None,
) -> MagicMock:
    p = MagicMock()
    p.id = provider_id
    p.name = name
    p.provider_type = provider_type
    p.endpoint_url = endpoint_url
    p.api_key = api_key
    p.priority = priority
    p.enabled = enabled
    p.ailb_sync_enabled = ailb_sync_enabled
    p.model_list = model_list or ["gpt-4o"]
    p.rate_limits = rate_limits or {}
    return p


def _make_deployment(
    deployment_id: int = 1,
    name: str = "local",
    endpoint_url: str = "http://node-1:11434",
    status: str = "running",
) -> MagicMock:
    d = MagicMock()
    d.id = deployment_id
    d.name = name
    d.endpoint_url = endpoint_url
    d.status = status
    return d


def _make_ollama_model(
    model_id: int = 1,
    deployment_id: int = 1,
    model_name: str = "llama3.2",
    model_tag: str = "latest",
) -> MagicMock:
    m = MagicMock()
    m.id = model_id
    m.deployment_id = deployment_id
    m.model_name = model_name
    m.model_tag = model_tag
    return m


def _make_key(
    key_id: int = 1,
    user_id: int = 1,
    org_id: int = 1,
    key_prefix: str = "wa-testke...",
    rpm_limit: int = 60,
    tpm_limit: int = 10000,
    budget_limit_daily: float = None,
    budget_limit_monthly: float = None,
    allowed_models: list = None,
    allowed_providers: list = None,
    expires_at: datetime = None,
    enabled: bool = True,
) -> MagicMock:
    k = MagicMock()
    k.id = key_id
    k.user_id = user_id
    k.organization_id = org_id
    k.name = f"Key {key_id}"
    k.key_prefix = key_prefix
    k.rpm_limit = rpm_limit
    k.tpm_limit = tpm_limit
    k.budget_limit_daily = budget_limit_daily
    k.budget_limit_monthly = budget_limit_monthly
    k.allowed_models = allowed_models
    k.allowed_providers = allowed_providers
    k.expires_at = expires_at
    k.enabled = enabled
    return k


# ---------------------------------------------------------------------------
# generate_full_config
# ---------------------------------------------------------------------------


class TestGenerateFullConfig:
    """Tests for MarchProxyConfigGenerator.generate_full_config"""

    def test_config_structure(self) -> None:
        """Full config has required top-level keys."""
        db = _make_db()
        db.return_value.select.return_value = make_select_result([])

        gen = MarchProxyConfigGenerator(db)
        config = gen.generate_full_config()

        assert config["version"] == "1.0"
        assert "generated_at" in config
        assert config["managed_by"] == "waddleai"
        ailb = config["ailb"]
        assert "providers" in ailb
        assert "routes" in ailb
        assert "rate_limits" in ailb
        assert "virtual_keys" in ailb

    def test_config_with_provider(self) -> None:
        """Provider is reflected in the providers list."""
        db = _make_db()
        provider = _make_provider()
        # Providers query (enabled+ailb_sync_enabled)
        db.return_value.select.return_value = make_select_result([provider])
        # Deployments query returns empty
        db.return_value.select.return_value.__iter__ = lambda s: iter([provider])

        gen = MarchProxyConfigGenerator(db)

        # Patch individual queries via side_effect
        call_results = [
            [provider],  # _generate_providers
            [provider],  # _generate_provider_routes (non-ollama providers)
            [],  # _generate_ollama_model_routes (deployments)
            [],  # _generate_rate_limits (keys)
            [],  # _generate_virtual_keys (keys)
        ]
        db.return_value.select.side_effect = call_results

        config = gen.generate_full_config()
        providers = config["ailb"]["providers"]
        assert len(providers) == 1
        assert providers[0]["name"] == "OpenAI"

    def test_config_empty_db(self) -> None:
        """Config with no DB rows returns empty lists."""
        db = _make_db()
        db.return_value.select.return_value = make_select_result([])

        gen = MarchProxyConfigGenerator(db)
        config = gen.generate_full_config()

        assert config["ailb"]["providers"] == []
        assert config["ailb"]["routes"] == []
        assert config["ailb"]["rate_limits"] == []
        assert config["ailb"]["virtual_keys"] == []


# ---------------------------------------------------------------------------
# _generate_providers
# ---------------------------------------------------------------------------


class TestGenerateProviders:
    """Tests for MarchProxyConfigGenerator._generate_providers"""

    def test_provider_with_api_key(self) -> None:
        """Provider with api_key includes auth block."""
        db = _make_db()
        provider = _make_provider(api_key="sk-real")
        db.return_value.select.return_value = make_select_result([provider])

        gen = MarchProxyConfigGenerator(db)
        providers = gen._generate_providers()

        assert len(providers) == 1
        assert providers[0]["auth"]["type"] == "api_key"
        assert providers[0]["auth"]["key"] == "sk-real"

    def test_provider_without_api_key(self) -> None:
        """Provider without api_key omits auth block."""
        db = _make_db()
        provider = _make_provider()
        provider.api_key = None
        db.return_value.select.return_value = make_select_result([provider])

        gen = MarchProxyConfigGenerator(db)
        providers = gen._generate_providers()

        assert "auth" not in providers[0]

    def test_provider_with_model_list(self) -> None:
        """model_list appears in provider config."""
        db = _make_db()
        provider = _make_provider(model_list=["gpt-4o", "gpt-3.5-turbo"])
        db.return_value.select.return_value = make_select_result([provider])

        gen = MarchProxyConfigGenerator(db)
        providers = gen._generate_providers()

        assert providers[0]["models"] == ["gpt-4o", "gpt-3.5-turbo"]

    def test_provider_metadata(self) -> None:
        """Provider metadata contains waddleai_provider_id."""
        db = _make_db()
        provider = _make_provider(provider_id=7)
        db.return_value.select.return_value = make_select_result([provider])

        gen = MarchProxyConfigGenerator(db)
        providers = gen._generate_providers()

        assert providers[0]["metadata"]["waddleai_provider_id"] == "7"

    def test_no_providers(self) -> None:
        """Empty select returns empty provider list."""
        db = _make_db()
        db.return_value.select.return_value = make_select_result([])

        gen = MarchProxyConfigGenerator(db)
        providers = gen._generate_providers()
        assert providers == []


# ---------------------------------------------------------------------------
# _generate_provider_routes
# ---------------------------------------------------------------------------


class TestGenerateProviderRoutes:
    """Tests for MarchProxyConfigGenerator._generate_provider_routes"""

    def test_openai_route_has_match_headers(self) -> None:
        """OpenAI route includes Authorization match header."""
        db = _make_db()
        provider = _make_provider(provider_type="openai")
        db.return_value.select.return_value = make_select_result([provider])

        gen = MarchProxyConfigGenerator(db)
        routes = gen._generate_provider_routes()

        assert len(routes) == 1
        assert "Authorization" in routes[0].get("match_headers", {})

    def test_anthropic_route_has_match_headers(self) -> None:
        """Anthropic route includes anthropic-version match header."""
        db = _make_db()
        provider = _make_provider(
            provider_type="anthropic",
            endpoint_url="https://api.anthropic.com",
        )
        provider.provider_type = "anthropic"
        db.return_value.select.return_value = make_select_result([provider])

        gen = MarchProxyConfigGenerator(db)
        routes = gen._generate_provider_routes()

        assert "anthropic-version" in routes[0].get("match_headers", {})

    def test_route_uses_https_protocol(self) -> None:
        """HTTPS endpoint sets protocol to https."""
        db = _make_db()
        provider = _make_provider(endpoint_url="https://api.openai.com/v1")
        db.return_value.select.return_value = make_select_result([provider])

        gen = MarchProxyConfigGenerator(db)
        routes = gen._generate_provider_routes()

        assert routes[0]["protocol"] == "https"

    def test_empty_providers(self) -> None:
        """No providers returns empty route list."""
        db = _make_db()
        db.return_value.select.return_value = make_select_result([])

        gen = MarchProxyConfigGenerator(db)
        routes = gen._generate_provider_routes()
        assert routes == []


# ---------------------------------------------------------------------------
# _generate_ollama_model_routes
# ---------------------------------------------------------------------------


class TestGenerateOllamaModelRoutes:
    """Tests for MarchProxyConfigGenerator._generate_ollama_model_routes"""

    def test_model_route_created_per_model(self) -> None:
        """One route per Ollama model per deployment."""
        db = _make_db()
        deployment = _make_deployment()
        model = _make_ollama_model()

        # Side effect order: deployments query; models query
        db.return_value.select.side_effect = [[deployment], [model]]

        gen = MarchProxyConfigGenerator(db)
        routes = gen._generate_ollama_model_routes()

        assert len(routes) == 1
        route = routes[0]
        assert route["destination"]["type"] == "ollama"
        assert route["match_conditions"]["body_json"]["model"] == "llama3.2"
        assert route["metadata"]["routing_type"] == "model-specific"

    def test_no_deployments_returns_empty(self) -> None:
        """No active deployments returns empty route list."""
        db = _make_db()
        db.return_value.select.return_value = make_select_result([])

        gen = MarchProxyConfigGenerator(db)
        routes = gen._generate_ollama_model_routes()
        assert routes == []


# ---------------------------------------------------------------------------
# _generate_rate_limits
# ---------------------------------------------------------------------------


class TestGenerateRateLimits:
    """Tests for MarchProxyConfigGenerator._generate_rate_limits"""

    def test_key_with_rpm_and_tpm(self) -> None:
        """Key with rpm_limit and tpm_limit generates limit entry."""
        db = _make_db()
        key = _make_key(rpm_limit=60, tpm_limit=10000)
        db.return_value.select.return_value = make_select_result([key])

        gen = MarchProxyConfigGenerator(db)
        limits = gen._generate_rate_limits()

        assert len(limits) == 1
        assert limits[0]["limits"]["requests_per_minute"] == 60
        assert limits[0]["limits"]["tokens_per_minute"] == 10000

    def test_key_with_budget_limits(self) -> None:
        """Budget limits appear in the limit entry."""
        db = _make_db()
        key = _make_key(budget_limit_daily=10.0, budget_limit_monthly=100.0)
        db.return_value.select.return_value = make_select_result([key])

        gen = MarchProxyConfigGenerator(db)
        limits = gen._generate_rate_limits()

        assert limits[0]["limits"]["cost_per_day_usd"] == 10.0
        assert limits[0]["limits"]["cost_per_month_usd"] == 100.0

    def test_key_without_limits_excluded(self) -> None:
        """Key with no rpm or tpm limits is not included."""
        db = _make_db()
        key = _make_key(rpm_limit=None, tpm_limit=None)
        db.return_value.select.return_value = make_select_result([key])

        gen = MarchProxyConfigGenerator(db)
        limits = gen._generate_rate_limits()
        assert limits == []

    def test_burst_size_calculated(self) -> None:
        """burst_size is max(10, rpm_limit // 6)."""
        db = _make_db()
        key = _make_key(rpm_limit=120, tpm_limit=None)
        db.return_value.select.return_value = make_select_result([key])

        gen = MarchProxyConfigGenerator(db)
        limits = gen._generate_rate_limits()

        assert limits[0]["limits"]["burst_size"] == 20  # 120 // 6

    def test_no_keys(self) -> None:
        """Empty keys returns empty list."""
        db = _make_db()
        db.return_value.select.return_value = make_select_result([])

        gen = MarchProxyConfigGenerator(db)
        limits = gen._generate_rate_limits()
        assert limits == []


# ---------------------------------------------------------------------------
# _generate_virtual_keys
# ---------------------------------------------------------------------------


class TestGenerateVirtualKeys:
    """Tests for MarchProxyConfigGenerator._generate_virtual_keys"""

    def test_basic_key_config(self) -> None:
        """Key entry contains required fields."""
        db = _make_db()
        key = _make_key()
        db.return_value.select.return_value = make_select_result([key])

        gen = MarchProxyConfigGenerator(db)
        keys = gen._generate_virtual_keys()

        assert len(keys) == 1
        assert keys[0]["key_prefix"] == "wa-testke..."
        assert keys[0]["enabled"] is True

    def test_key_with_allowed_models(self) -> None:
        """allowed_models appears in key config."""
        db = _make_db()
        key = _make_key(allowed_models=["gpt-4o"])
        db.return_value.select.return_value = make_select_result([key])

        gen = MarchProxyConfigGenerator(db)
        keys = gen._generate_virtual_keys()

        assert keys[0]["allowed_models"] == ["gpt-4o"]

    def test_key_with_budget_limits(self) -> None:
        """Budget limits appear in key config."""
        db = _make_db()
        key = _make_key(budget_limit_daily=5.0, budget_limit_monthly=50.0)
        db.return_value.select.return_value = make_select_result([key])

        gen = MarchProxyConfigGenerator(db)
        keys = gen._generate_virtual_keys()

        bl = keys[0]["budget_limits"]
        assert bl["daily_usd"] == 5.0
        assert bl["monthly_usd"] == 50.0

    def test_key_with_expiration(self) -> None:
        """Expiring keys include expires_at in ISO format."""
        db = _make_db()
        exp = datetime(2026, 1, 1, 0, 0, 0)
        key = _make_key(expires_at=exp)
        db.return_value.select.return_value = make_select_result([key])

        gen = MarchProxyConfigGenerator(db)
        keys = gen._generate_virtual_keys()

        assert "expires_at" in keys[0]

    def test_no_keys(self) -> None:
        """Empty keys returns empty list."""
        db = _make_db()
        db.return_value.select.return_value = make_select_result([])

        gen = MarchProxyConfigGenerator(db)
        keys = gen._generate_virtual_keys()
        assert keys == []


# ---------------------------------------------------------------------------
# generate_ollama_routing_table
# ---------------------------------------------------------------------------


class TestGenerateOllamaRoutingTable:
    """Tests for MarchProxyConfigGenerator.generate_ollama_routing_table"""

    def test_routing_table_model_to_endpoint(self) -> None:
        """Routing table maps model name to deployment endpoint."""
        db = _make_db()
        deployment = _make_deployment(endpoint_url="http://node-1:11434")
        model = _make_ollama_model(model_name="llama3.2", model_tag="latest")
        db.return_value.select.side_effect = [[deployment], [model]]

        gen = MarchProxyConfigGenerator(db)
        table = gen.generate_ollama_routing_table()

        assert "llama3.2:latest" in table
        assert table["llama3.2:latest"] == "http://node-1:11434"

    def test_routing_table_no_tag(self) -> None:
        """Model with no tag uses plain model_name as key."""
        db = _make_db()
        deployment = _make_deployment()
        model = _make_ollama_model(model_tag=None)
        model.model_tag = None
        db.return_value.select.side_effect = [[deployment], [model]]

        gen = MarchProxyConfigGenerator(db)
        table = gen.generate_ollama_routing_table()

        assert "llama3.2" in table

    def test_routing_table_empty(self) -> None:
        """No deployments returns empty table."""
        db = _make_db()
        db.return_value.select.return_value = make_select_result([])

        gen = MarchProxyConfigGenerator(db)
        table = gen.generate_ollama_routing_table()
        assert table == {}


# ---------------------------------------------------------------------------
# export_to_file
# ---------------------------------------------------------------------------


class TestExportToFile:
    """Tests for MarchProxyConfigGenerator.export_to_file"""

    def test_export_creates_file(self) -> None:
        """export_to_file writes valid JSON to disk."""
        db = _make_db()
        db.return_value.select.return_value = make_select_result([])

        gen = MarchProxyConfigGenerator(db)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name

        try:
            result = gen.export_to_file(path)
            assert result is True
            with open(path) as fh:
                data = json.load(fh)
            assert data["version"] == "1.0"
        finally:
            os.unlink(path)

    def test_export_returns_false_on_error(self) -> None:
        """export_to_file returns False if the path is invalid."""
        db = _make_db()
        db.return_value.select.return_value = make_select_result([])

        gen = MarchProxyConfigGenerator(db)
        result = gen.export_to_file("/nonexistent/path/config.json")
        assert result is False


# ---------------------------------------------------------------------------
# generate_model_routing_config
# ---------------------------------------------------------------------------


class TestGenerateModelRoutingConfig:
    """Tests for MarchProxyConfigGenerator.generate_model_routing_config"""

    def test_routing_config_structure(self) -> None:
        """Routing config has required keys."""
        db = _make_db()
        db.return_value.select.return_value = make_select_result([])

        gen = MarchProxyConfigGenerator(db)
        config = gen.generate_model_routing_config()

        assert config["version"] == "1.0"
        assert config["routing_strategy"] == "model-aware"
        assert "model_routes" in config
        assert "health_check" in config

    def test_routing_config_with_model(self) -> None:
        """Model routes include deployment info."""
        db = _make_db()
        deployment = _make_deployment()
        model = _make_ollama_model(model_name="mistral")
        db.return_value.select.side_effect = [[deployment], [model]]

        gen = MarchProxyConfigGenerator(db)
        config = gen.generate_model_routing_config()

        assert "mistral" in config["model_routes"]
        routes = config["model_routes"]["mistral"]
        assert len(routes) == 1
        assert routes[0]["endpoint"] == "http://node-1:11434"
