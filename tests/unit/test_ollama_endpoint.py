"""Direct unit tests for `shared.utils.ollama_endpoint`.

Covers the canonical/legacy/default precedence chain for both the chat and
embedding resolvers, the module-level `embedding_endpoint_label`, and the
one-time log/deprecation-warn emission. `shared.utils.embedding_manager`
delegates to this module (see `tests/unit/test_embedding_manager.py`), which
exercises the chain indirectly through `resolve_embedding_ollama_host`.
"""

from __future__ import annotations

import pytest

from shared.utils import ollama_endpoint as oe


@pytest.fixture(autouse=True)
def _clean_env_and_log_state(monkeypatch: pytest.MonkeyPatch):
    """Unset every chat/embedding env var and reset the one-time log flags."""
    for name in (oe.CANONICAL_CHAT_URL_ENV, *oe.LEGACY_CHAT_URL_ENVS):
        monkeypatch.delenv(name, raising=False)
    for name in (oe.CANONICAL_EMBEDDING_URL_ENV, *oe.LEGACY_EMBEDDING_URL_ENVS):
        monkeypatch.delenv(name, raising=False)
    oe._logged_chat = False
    oe._logged_embedding = False
    yield
    oe._logged_chat = False
    oe._logged_embedding = False


class TestResolveOllamaUrl:
    """`resolve_ollama_url`'s canonical/legacy/default precedence chain."""

    def test_default_when_nothing_set(self) -> None:
        """No env vars set resolves to the module default."""
        assert oe.resolve_ollama_url() == oe.DEFAULT_OLLAMA_URL

    def test_custom_default_honored(self) -> None:
        """A caller-supplied default wins when nothing else is set."""
        assert oe.resolve_ollama_url(default="http://custom-default:11434") == (
            "http://custom-default:11434"
        )

    @pytest.mark.parametrize("env_name", oe.LEGACY_CHAT_URL_ENVS)
    def test_each_legacy_name_resolves(
        self, env_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every legacy chat env var, set alone, resolves to its value."""
        monkeypatch.setenv(env_name, "http://legacy:11434")
        assert oe.resolve_ollama_url() == "http://legacy:11434"

    def test_canonical_beats_every_legacy_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The canonical var wins even when every legacy var is also set."""
        for env_name in oe.LEGACY_CHAT_URL_ENVS:
            monkeypatch.setenv(env_name, "http://legacy:11434")
        monkeypatch.setenv(oe.CANONICAL_CHAT_URL_ENV, "http://canonical:11434")
        assert oe.resolve_ollama_url() == "http://canonical:11434"

    def test_legacy_chain_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`OLLAMA_API_URL` > `OLLAMA_URL` > `OLLAMA_HOST` > `OLLAMA_BASE_URL`."""
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://fourth:11434")
        assert oe.resolve_ollama_url() == "http://fourth:11434"
        monkeypatch.setenv("OLLAMA_HOST", "http://third:11434")
        assert oe.resolve_ollama_url() == "http://third:11434"
        monkeypatch.setenv("OLLAMA_URL", "http://second:11434")
        assert oe.resolve_ollama_url() == "http://second:11434"
        monkeypatch.setenv("OLLAMA_API_URL", "http://first:11434")
        assert oe.resolve_ollama_url() == "http://first:11434"

    def test_empty_string_env_value_is_treated_as_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty-string canonical value is skipped in favor of a legacy one."""
        monkeypatch.setenv(oe.CANONICAL_CHAT_URL_ENV, "")
        monkeypatch.setenv("OLLAMA_API_URL", "http://legacy:11434")
        assert oe.resolve_ollama_url() == "http://legacy:11434"


class TestResolveOllamaEmbeddingUrl:
    """`resolve_ollama_embedding_url`'s canonical/legacy/chat-fallback chain."""

    def test_falls_back_to_resolve_ollama_url_when_no_chat_url_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No `chat_url` argument triggers a full `resolve_ollama_url()` call."""
        monkeypatch.setenv(oe.CANONICAL_CHAT_URL_ENV, "http://chat:11434")
        assert oe.resolve_ollama_embedding_url() == "http://chat:11434"

    def test_falls_back_to_explicit_chat_url_argument(self) -> None:
        """An explicit `chat_url` argument is used verbatim as the fallback."""
        assert oe.resolve_ollama_embedding_url(chat_url="http://explicit-chat:11434") == (
            "http://explicit-chat:11434"
        )

    def test_empty_string_chat_url_argument_bypasses_default_default(self) -> None:
        """`chat_url=""` means "no fallback".

        Used by callers that want an empty string back when no dedicated
        embedding endpoint is configured.
        """
        assert oe.resolve_ollama_embedding_url(chat_url="") == ""

    @pytest.mark.parametrize("env_name", oe.LEGACY_EMBEDDING_URL_ENVS)
    def test_each_legacy_embedding_name_resolves(
        self, env_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every legacy embedding env var, set alone, resolves to its value."""
        monkeypatch.setenv(env_name, "http://legacy-embed:11434")
        assert oe.resolve_ollama_embedding_url(chat_url="http://chat:11434") == (
            "http://legacy-embed:11434"
        )

    def test_canonical_embedding_beats_legacy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The canonical embedding var wins even when every legacy var is set."""
        for env_name in oe.LEGACY_EMBEDDING_URL_ENVS:
            monkeypatch.setenv(env_name, "http://legacy-embed:11434")
        monkeypatch.setenv(oe.CANONICAL_EMBEDDING_URL_ENV, "http://canonical-embed:11434")
        assert oe.resolve_ollama_embedding_url(chat_url="http://chat:11434") == (
            "http://canonical-embed:11434"
        )


class TestEmbeddingEndpointLabel:
    """`embedding_endpoint_label`'s bounded two-value metric label."""

    def test_chat_ollama_when_nothing_set(self) -> None:
        """No dedicated embedding endpoint configured labels as chat_ollama."""
        assert oe.embedding_endpoint_label() == "chat_ollama"

    def test_embedding_ollama_when_canonical_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The canonical embedding var configured labels as embedding_ollama."""
        monkeypatch.setenv(oe.CANONICAL_EMBEDDING_URL_ENV, "http://embed:11434")
        assert oe.embedding_endpoint_label() == "embedding_ollama"

    @pytest.mark.parametrize("env_name", oe.LEGACY_EMBEDDING_URL_ENVS)
    def test_embedding_ollama_when_legacy_set(
        self, env_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Any legacy embedding var configured also labels as embedding_ollama."""
        monkeypatch.setenv(env_name, "http://embed:11434")
        assert oe.embedding_endpoint_label() == "embedding_ollama"


class TestLogOnceAndDeprecationWarning:
    """One-time INFO resolution log and one-time legacy-name DEPRECATION warn."""

    def test_chat_resolution_logs_info_once(self, caplog: pytest.LogCaptureFixture) -> None:
        """Repeated resolution calls emit exactly one INFO record."""
        with caplog.at_level("INFO", logger="shared.utils.ollama_endpoint"):
            oe.resolve_ollama_url()
            oe.resolve_ollama_url()
            oe.resolve_ollama_url()
        info_records = [r for r in caplog.records if r.levelname == "INFO"]
        assert len(info_records) == 1

    def test_legacy_chat_name_warns_deprecation_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A legacy chat var triggers exactly one DEPRECATION warning naming the canonical var."""
        monkeypatch.setenv("OLLAMA_HOST", "http://legacy:11434")
        with caplog.at_level("WARNING", logger="shared.utils.ollama_endpoint"):
            oe.resolve_ollama_url()
            oe.resolve_ollama_url()
        warnings = [r for r in caplog.records if "DEPRECATED" in r.message]
        assert len(warnings) == 1
        assert oe.CANONICAL_CHAT_URL_ENV in warnings[0].message

    def test_canonical_chat_name_never_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The canonical chat var never triggers a DEPRECATION warning."""
        monkeypatch.setenv(oe.CANONICAL_CHAT_URL_ENV, "http://canonical:11434")
        with caplog.at_level("WARNING", logger="shared.utils.ollama_endpoint"):
            oe.resolve_ollama_url()
        assert not [r for r in caplog.records if "DEPRECATED" in r.message]

    def test_default_resolution_never_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """Falling back to the bare default never triggers a DEPRECATION warning."""
        with caplog.at_level("WARNING", logger="shared.utils.ollama_endpoint"):
            oe.resolve_ollama_url()
        assert not [r for r in caplog.records if "DEPRECATED" in r.message]

    def test_legacy_embedding_name_warns_deprecation_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A legacy embedding var triggers one DEPRECATION warning naming the canonical var."""
        monkeypatch.setenv("OLLAMA_EMBEDDING_URL", "http://legacy-embed:11434")
        with caplog.at_level("WARNING", logger="shared.utils.ollama_endpoint"):
            oe.resolve_ollama_embedding_url()
            oe.resolve_ollama_embedding_url()
        warnings = [r for r in caplog.records if "DEPRECATED" in r.message]
        assert len(warnings) == 1
        assert oe.CANONICAL_EMBEDDING_URL_ENV in warnings[0].message
