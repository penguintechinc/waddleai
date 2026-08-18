"""Ollama/llama.cpp KV session-affinity map + router preferred_backend hint (spec §6.3)."""

from unittest.mock import MagicMock

from shared.cache.affinity import SessionAffinityMap
from shared.utils.llm_connectors import LlamaCppConnector, OllamaConnector
from shared.utils.request_router import LLMRequestRouter, ProviderStats


class TestSessionAffinityMap:
    """Tests for session affinity map."""

    async def test_record_then_lookup_round_trips(self, fake_valkey):
        """Record then lookup round trips."""
        affinity = SessionAffinityMap(fake_valkey)
        await affinity.record(org_id=1, session_hash="sess-a", backend_id="ollama-pod-2")

        result = await affinity.lookup(org_id=1, session_hash="sess-a")
        assert result == "ollama-pod-2"

    async def test_lookup_after_ttl_expiry_returns_none(self, fake_valkey):
        """Lookup after ttl expiry returns none."""
        affinity = SessionAffinityMap(fake_valkey, ttl_seconds=100)
        fake_valkey.now = lambda: 0
        await affinity.record(org_id=1, session_hash="sess-a", backend_id="ollama-pod-2")

        fake_valkey.now = lambda: 101
        result = await affinity.lookup(org_id=1, session_hash="sess-a")
        assert result is None

    async def test_lookup_slides_ttl_forward(self, fake_valkey):
        """Lookup slides ttl forward."""
        affinity = SessionAffinityMap(fake_valkey, ttl_seconds=100)
        fake_valkey.now = lambda: 0
        await affinity.record(org_id=1, session_hash="sess-a", backend_id="ollama-pod-2")

        fake_valkey.now = lambda: 90  # still within TTL
        assert await affinity.lookup(org_id=1, session_hash="sess-a") == "ollama-pod-2"

        # Without the slide, this would now be past the original 100s window.
        fake_valkey.now = lambda: 150
        assert await affinity.lookup(org_id=1, session_hash="sess-a") == "ollama-pod-2"

    async def test_keys_are_org_namespaced(self, fake_valkey):
        """Keys are org namespaced."""
        affinity = SessionAffinityMap(fake_valkey)
        await affinity.record(org_id=1, session_hash="sess-shared", backend_id="ollama-pod-1")

        result_other_org = await affinity.lookup(org_id=2, session_hash="sess-shared")
        assert result_other_org is None


def _router_with_connectors(connectors: dict, stats: dict = None) -> LLMRequestRouter:
    """Router with connectors."""
    manager = MagicMock()
    manager.connectors = connectors
    router = LLMRequestRouter(llm_manager=manager, db=MagicMock())
    if stats:
        router.provider_stats = stats
    return router


class TestRouterPreferredBackendHint:
    """Tests for router preferred backend hint."""

    def test_preferred_backend_selected_when_healthy_ollama(self):
        """Preferred backend selected when healthy ollama."""
        ollama_connector = MagicMock(spec=OllamaConnector)
        ollama_connector.model_list = []
        other_connector = MagicMock(spec=OllamaConnector)
        other_connector.model_list = []
        router = _router_with_connectors(
            {"ollama-a": ollama_connector, "ollama-b": other_connector}
        )

        result = router.select_provider("llama3", preferred_backend="ollama-b")
        assert result == ("ollama-b", "llama3")

    def test_preferred_backend_ignored_when_circuit_broken(self):
        """Preferred backend ignored when circuit broken."""
        from datetime import datetime

        ollama_a = MagicMock(spec=OllamaConnector)
        ollama_a.model_list = []
        ollama_b = MagicMock(spec=OllamaConnector)
        ollama_b.model_list = []
        stats = {
            "ollama-a": ProviderStats(),
            "ollama-b": ProviderStats(consecutive_failures=10, last_failure=datetime.utcnow()),
        }
        router = _router_with_connectors({"ollama-a": ollama_a, "ollama-b": ollama_b}, stats=stats)

        result = router.select_provider("llama3", preferred_backend="ollama-b")
        # ollama-b is tripped/unavailable; must never be selected via the hint.
        assert result is not None
        assert result[0] == "ollama-a"

    def test_preferred_backend_ignored_for_non_ollama_llamacpp_provider(self):
        """Preferred backend ignored for non ollama llamacpp provider."""
        openai_connector = MagicMock()  # not spec'd to Ollama/LlamaCpp -> isinstance() is False
        openai_connector.model_list = []
        router = _router_with_connectors({"openai": openai_connector})

        result = router.select_provider("gpt-4", preferred_backend="openai")
        # Falls through to normal strategy selection, which still picks the
        # only available provider -- but not *because* of the hint.
        assert result == ("openai", "gpt-4")

    def test_preferred_backend_honored_for_llamacpp(self):
        """Preferred backend honored for llamacpp."""
        llamacpp_connector = MagicMock(spec=LlamaCppConnector)
        llamacpp_connector.model_list = []
        other = MagicMock(spec=LlamaCppConnector)
        other.model_list = []
        router = _router_with_connectors({"llamacpp-a": llamacpp_connector, "llamacpp-b": other})

        result = router.select_provider("local-model", preferred_backend="llamacpp-b")
        assert result == ("llamacpp-b", "local-model")

    def test_no_preferred_backend_falls_back_to_normal_selection(self):
        """No preferred backend falls back to normal selection."""
        connector = MagicMock(spec=OllamaConnector)
        connector.model_list = []
        router = _router_with_connectors({"ollama-a": connector})

        result = router.select_provider("llama3")
        assert result == ("ollama-a", "llama3")

    def test_preferred_backend_not_in_available_set_falls_back(self):
        """Preferred backend not in available set falls back."""
        connector = MagicMock(spec=OllamaConnector)
        connector.model_list = []
        router = _router_with_connectors({"ollama-a": connector})

        result = router.select_provider("llama3", preferred_backend="ollama-nonexistent")
        assert result == ("ollama-a", "llama3")
