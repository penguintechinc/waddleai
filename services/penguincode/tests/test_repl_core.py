"""Coverage for the `REPLSession` core surface in `penguincode_cli.core.repl`.

Drives the real class (real `__init__`, real command dispatcher) with every external
boundary faked: the gRPC `KnowledgeClient`/`LessonsClient`, `OllamaClient`, the agents, the
org-config HTTP client and `prompt_toolkit`. No network, no live Ollama, no live server.

# regression: penguincode-repl-core-coverage
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from penguincode_cli.client.knowledge_client import (
    KnowledgeClientError,
    KnowledgeServerUnavailableError,
)
from penguincode_cli.config.settings import MCPServerConfig, Settings
from penguincode_cli.core import repl as repl_mod
from penguincode_cli.core.repl import REPLSession
from penguincode_cli.docs_rag.models import Language, Library, ProjectContext


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> REPLSession:
    """A real `REPLSession` rooted in `tmp_path`, HOME redirected, no config file present."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    with patch.object(repl_mod, "load_settings", side_effect=FileNotFoundError):
        s = REPLSession(str(tmp_path), str(tmp_path / "missing.yaml"))
    s.settings.docs_rag.cache_dir = str(tmp_path / "docs-cache")
    return s


def _lib(
    name: str = "requests", lang: Language = Language.PYTHON, ver: str | None = "1.0"
) -> Library:
    return Library(name=name, language=lang, version=ver)


class TestInit:
    def test_missing_config_falls_back_to_defaults(self, session: REPLSession) -> None:
        assert isinstance(session.settings, Settings)
        assert session.connectivity_ok is True
        assert session.knowledge_client is None and session.lessons_client is None

    def test_present_config_is_used(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        sentinel = Settings()
        with patch.object(repl_mod, "load_settings", return_value=sentinel):
            s = REPLSession(str(tmp_path), "c.yaml")
        assert s.settings is sentinel


class TestConnectivity:
    def test_no_change_is_silent(self, session: REPLSession) -> None:
        with (
            patch.object(repl_mod, "print_error") as err,
            patch.object(repl_mod, "print_success") as ok,
        ):
            session._note_connectivity(ok=True)
        err.assert_not_called()
        ok.assert_not_called()

    def test_down_then_up_notices(self, session: REPLSession) -> None:
        with (
            patch.object(repl_mod, "print_error") as err,
            patch.object(repl_mod, "print_success") as ok,
        ):
            session._note_connectivity(ok=False)
            assert session.connectivity_ok is False
            assert "unreachable" in err.call_args[0][0]
            session._note_connectivity(ok=True)
            assert "Reconnected" in ok.call_args[0][0]
        assert session.connectivity_ok is True


def _agent_patches() -> Any:
    chat = MagicMock()
    chat.return_value._get_mcp_tools = AsyncMock(return_value=([1, 2], {}))
    return (
        patch("penguincode_cli.agents.ChatAgent", chat),
        patch("penguincode_cli.agents.ExecutorAgent", MagicMock()),
        patch("penguincode_cli.agents.ExplorerAgent", MagicMock()),
        chat,
    )


class TestAenter:
    async def _enter(self, s: REPLSession, chat: MagicMock) -> None:
        pc, pe, px, _ = _agent_patches()
        ollama = MagicMock()
        ollama.return_value.__aenter__ = AsyncMock()
        with (
            patch.object(repl_mod, "OllamaClient", ollama),
            patch.object(repl_mod, "KnowledgeClient", MagicMock()),
            patch.object(repl_mod, "LessonsClient", MagicMock()),
            patch("penguincode_cli.agents.ChatAgent", chat),
            pe,
            px,
            patch.object(s, "_fetch_org_config", AsyncMock()),
            patch.object(s, "_init_docs_rag", AsyncMock()) as docs,
            patch.object(repl_mod, "print_info") as info,
        ):
            self.docs = docs
            self.info = info
            assert await s.__aenter__() is s

    async def test_full_startup_memory_mcp_and_docs(self, session: REPLSession) -> None:
        session.settings.memory.enabled = True
        session.settings.docs_rag.enabled = True
        session.settings.mcp.enabled = True
        session.settings.mcp.servers = [MCPServerConfig(name="a")]
        chat = MagicMock()
        chat.return_value._get_mcp_tools = AsyncMock(return_value=([1, 2], {}))
        await self._enter(session, chat)
        assert session.memory_manager is not None
        assert set(session.agents) == {"executor", "explorer"}
        self.docs.assert_awaited_once()
        assert any("MCP: 2 tool(s)" in c[0][0] for c in self.info.call_args_list)

    async def test_mcp_empty_and_memory_docs_disabled(self, session: REPLSession) -> None:
        session.settings.memory.enabled = False
        session.settings.docs_rag.enabled = False
        session.settings.mcp.enabled = True
        session.settings.mcp.servers = [MCPServerConfig(name="a")]
        chat = MagicMock()
        chat.return_value._get_mcp_tools = AsyncMock(return_value=None)
        await self._enter(session, chat)
        assert session.memory_manager is None
        self.docs.assert_not_awaited()

    async def test_mcp_discovery_failure_is_non_fatal(self, session: REPLSession) -> None:
        session.settings.mcp.enabled = True
        session.settings.mcp.servers = [MCPServerConfig(name="a")]
        chat = MagicMock()
        chat.return_value._get_mcp_tools = AsyncMock(side_effect=RuntimeError("mcp down"))
        await self._enter(session, chat)
        assert any("MCP discovery skipped" in c[0][0] for c in self.info.call_args_list)


class _Org:
    def __init__(
        self, auth: bool = True, servers: list | None = None, boom: Exception | None = None
    ):
        self._auth, self._servers, self._boom = auth, servers or [], boom

    def __call__(self, **_: Any) -> _Org:
        return self

    async def authenticate(self) -> bool:
        if self._boom:
            raise self._boom
        return self._auth

    async def fetch_all(self) -> dict[str, Any]:
        return {"mcp_servers": self._servers}


class TestOrgConfig:
    async def test_no_server_url_noop(self, session: REPLSession) -> None:
        session.settings.client.server_url = ""
        await session._fetch_org_config()

    async def test_auth_failure_skips(self, session: REPLSession) -> None:
        session.settings.client.server_url = "http://x"
        with (
            patch("penguincode_cli.client.org_config.OrgConfigClient", _Org(auth=False)),
            patch.object(repl_mod, "print_info") as info,
        ):
            await session._fetch_org_config()
        assert "auth failed" in info.call_args[0][0]

    async def test_merges_new_servers_local_wins(self, session: REPLSession) -> None:
        session.settings.client.server_url = "http://x"
        session.settings.mcp.servers = [MCPServerConfig(name="local")]
        servers = [
            {"name": "local"},
            {"name": "org", "enabled": False, "transport": "http", "url": "u"},
            {"nameless": 1},
            "not-a-dict",
        ]
        with (
            patch("penguincode_cli.client.org_config.OrgConfigClient", _Org(servers=servers)),
            patch.object(repl_mod, "print_info") as info,
        ):
            await session._fetch_org_config()
        assert [s.name for s in session.settings.mcp.servers] == ["local", "org"]
        assert "merged 4" in info.call_args[0][0]

    async def test_no_org_servers(self, session: REPLSession) -> None:
        session.settings.client.server_url = "http://x"
        with patch("penguincode_cli.client.org_config.OrgConfigClient", _Org()):
            await session._fetch_org_config()
        assert session.settings.mcp.servers == []

    async def test_import_error_swallowed(self, session: REPLSession) -> None:
        session.settings.client.server_url = "http://x"
        with patch.dict(sys.modules, {"penguincode_cli.client.org_config": None}):
            await session._fetch_org_config()

    async def test_other_error_reported(self, session: REPLSession) -> None:
        session.settings.client.server_url = "http://x"
        with (
            patch(
                "penguincode_cli.client.org_config.OrgConfigClient", _Org(boom=RuntimeError("nope"))
            ),
            patch.object(repl_mod, "print_info") as info,
        ):
            await session._fetch_org_config()
        assert "unavailable: nope" in info.call_args[0][0]


class TestInitDocsRag:
    async def test_detects_merges_manual_and_cleans(
        self, session: REPLSession, tmp_path: Path
    ) -> None:
        (tmp_path / "requirements.txt").write_text("requests==2.0\n")
        session.knowledge_client = MagicMock()
        session.settings.docs_rag.languages_manual = {
            "python": True,
            "rust": True,
            "bogus": True,
            "go": False,
        }
        session.settings.docs_rag.auto_index_on_detect = True
        fetcher = MagicMock()
        fetcher.expunge_expired.return_value = 3
        fetcher.cleanup_unused_libraries.return_value = {"old": 1}
        with (
            patch("penguincode_cli.docs_rag.DocumentationFetcher", return_value=fetcher),
            patch.object(session, "_auto_index_languages", AsyncMock()) as auto,
            patch.object(repl_mod, "print_error") as err,
            patch.object(repl_mod, "print_info") as info,
        ):
            await session._init_docs_rag()
        auto.assert_awaited_once()
        assert any("Unknown language" in c[0][0] for c in err.call_args_list)
        texts = " ".join(c[0][0] for c in info.call_args_list)
        assert "Detected:" in texts and "Cleaned up 3" in texts and "Removed docs" in texts
        assert Language.RUST in session.project_context.languages
        assert session.context_injector is not None

    async def test_manual_only_no_auto_detect(self, session: REPLSession) -> None:
        session.knowledge_client = MagicMock()
        session.settings.docs_rag.auto_detect_on_start = False
        session.settings.docs_rag.languages_manual = {"python": True}
        fetcher = MagicMock()
        fetcher.expunge_expired.return_value = 0
        fetcher.cleanup_unused_libraries.return_value = {}
        with patch("penguincode_cli.docs_rag.DocumentationFetcher", return_value=fetcher):
            await session._init_docs_rag()
        assert session.project_context.languages == [Language.PYTHON]

    async def test_detect_with_no_languages_is_quiet(self, session: REPLSession) -> None:
        session.knowledge_client = MagicMock()
        session.settings.docs_rag.languages_manual = {}
        fetcher = MagicMock()
        fetcher.expunge_expired.return_value = 0
        fetcher.cleanup_unused_libraries.return_value = {}
        with (
            patch("penguincode_cli.docs_rag.DocumentationFetcher", return_value=fetcher),
            patch("penguincode_cli.docs_rag.ProjectDetector") as det,
            patch.object(repl_mod, "print_info") as info,
        ):
            det.return_value.detect.return_value = ProjectContext()
            await session._init_docs_rag()
        info.assert_not_called()

    async def test_import_error_reported(self, session: REPLSession) -> None:
        with (
            patch.dict(sys.modules, {"penguincode_cli.docs_rag": None}),
            patch.object(repl_mod, "print_info") as info,
        ):
            await session._init_docs_rag()
        assert "Docs RAG not available" in info.call_args[0][0]

    async def test_init_failure_reported(self, session: REPLSession) -> None:
        session.knowledge_client = MagicMock()
        with (
            patch("penguincode_cli.docs_rag.DocumentationFetcher", side_effect=RuntimeError("bad")),
            patch.object(repl_mod, "print_error") as err,
        ):
            await session._init_docs_rag()
        assert "init failed: bad" in err.call_args[0][0]


class TestAutoIndex:
    async def test_guard_returns_when_unready(self, session: REPLSession) -> None:
        await session._auto_index_languages()  # nothing set -> early return

    async def test_indexes_skips_and_tolerates_failures(self, session: REPLSession) -> None:
        session.project_context = ProjectContext(
            languages=[Language.PYTHON, Language.GO, Language.RUST]
        )
        session.docs_fetcher = MagicMock()
        session.docs_fetcher.fetch_language_docs = AsyncMock(side_effect=[["d"], [], ["e"]])
        session.knowledge_client = MagicMock()
        session.knowledge_client.index = AsyncMock(
            side_effect=[SimpleNamespace(chunks_indexed=4), KnowledgeClientError("x")]
        )
        sources = {Language.PYTHON: "src", Language.GO: "src", Language.RUST: "src"}
        with (
            patch(
                "penguincode_cli.docs_rag.get_language_doc_source",
                side_effect=lambda lang: sources.get(lang),
            ),
            patch.object(repl_mod, "print_info") as info,
        ):
            await session._auto_index_languages()
        assert "Auto-indexed 4" in info.call_args[0][0]

    async def test_language_without_doc_source_skipped(self, session: REPLSession) -> None:
        session.project_context = ProjectContext(languages=[Language.PYTHON])
        session.docs_fetcher = MagicMock()
        session.knowledge_client = MagicMock()
        with patch("penguincode_cli.docs_rag.get_language_doc_source", return_value=None):
            await session._auto_index_languages()
        session.docs_fetcher.fetch_language_docs.assert_not_called()


class TestEnsureIndexed:
    async def test_not_ready(self, session: REPLSession) -> None:
        assert await session._ensure_language_indexed("python") is False

    async def test_unknown_language(self, session: REPLSession) -> None:
        session.docs_fetcher, session.knowledge_client = MagicMock(), MagicMock()
        assert await session._ensure_language_indexed("klingon") is False

    async def test_no_doc_source(self, session: REPLSession) -> None:
        session.docs_fetcher, session.knowledge_client = MagicMock(), MagicMock()
        with patch("penguincode_cli.docs_rag.get_language_doc_source", return_value=None):
            assert await session._ensure_language_indexed("python") is False

    async def test_success(self, session: REPLSession) -> None:
        session.docs_fetcher = MagicMock()
        session.docs_fetcher.fetch_language_docs = AsyncMock(return_value=["d"])
        session.knowledge_client = MagicMock()
        session.knowledge_client.index = AsyncMock(return_value=SimpleNamespace(chunks_indexed=2))
        with patch("penguincode_cli.docs_rag.get_language_doc_source", return_value="s"):
            assert await session._ensure_language_indexed("Python") is True

    async def test_no_docs_fetched(self, session: REPLSession) -> None:
        session.docs_fetcher = MagicMock()
        session.docs_fetcher.fetch_language_docs = AsyncMock(return_value=[])
        session.knowledge_client = MagicMock()
        with patch("penguincode_cli.docs_rag.get_language_doc_source", return_value="s"):
            assert await session._ensure_language_indexed("python") is False

    async def test_index_error(self, session: REPLSession) -> None:
        session.docs_fetcher = MagicMock()
        session.docs_fetcher.fetch_language_docs = AsyncMock(return_value=["d"])
        session.knowledge_client = MagicMock()
        session.knowledge_client.index = AsyncMock(side_effect=KnowledgeClientError("x"))
        with patch("penguincode_cli.docs_rag.get_language_doc_source", return_value="s"):
            assert await session._ensure_language_indexed("python") is False


class TestAexitAndUpdateCheck:
    async def test_aexit_closes_everything(self, session: REPLSession) -> None:
        session.chat_agent = MagicMock(shutdown=AsyncMock())
        session.ollama_client = MagicMock(__aexit__=AsyncMock())
        session.knowledge_client = MagicMock(close=AsyncMock())
        session.lessons_client = MagicMock(close=AsyncMock())
        session._update_check_task = asyncio.create_task(asyncio.sleep(60))
        await session.__aexit__(None, None, None)
        session.chat_agent.shutdown.assert_awaited_once()
        session.knowledge_client.close.assert_awaited_once()
        session.lessons_client.close.assert_awaited_once()
        assert session._update_check_task.cancelled()

    async def test_aexit_with_nothing_initialised(self, session: REPLSession) -> None:
        await session.__aexit__(None, None, None)

    async def test_update_check_prints_notice(self, session: REPLSession) -> None:
        with (
            patch.object(repl_mod, "maybe_notify_update", AsyncMock(return_value="new!")),
            patch.object(repl_mod, "print_info") as info,
        ):
            await session._check_for_update()
        info.assert_called_once_with("new!")

    async def test_update_check_silent_when_current(self, session: REPLSession) -> None:
        with (
            patch.object(repl_mod, "maybe_notify_update", AsyncMock(return_value=None)),
            patch.object(repl_mod, "print_info") as info,
        ):
            await session._check_for_update()
        info.assert_not_called()

    async def test_update_check_swallows_errors(self, session: REPLSession) -> None:
        with (
            patch.object(repl_mod, "maybe_notify_update", AsyncMock(side_effect=RuntimeError("x"))),
            patch.object(repl_mod, "print_info") as info,
        ):
            await session._check_for_update()
        info.assert_not_called()


class TestDispatcher:
    @pytest.mark.parametrize(
        ("cmd", "target"),
        [
            ("/help", "show_help"),
            ("/history", "show_history"),
            ("/agents", "show_agents"),
            ("/read f", "handle_read"),
            ("/explore q", "handle_explore"),
            ("/execute t", "handle_execute"),
            ("/docs status", "handle_docs_command"),
            ("/index-code", "handle_index_code"),
            ("/lesson pending", "handle_lesson_command"),
            ("/skill", "handle_skill_command"),
            ("/skills", "handle_skill_command"),
            ("/config", "handle_config_command"),
        ],
    )
    async def test_routes(self, session: REPLSession, cmd: str, target: str) -> None:
        mock = (
            AsyncMock()
            if target.startswith(
                ("handle_read", "handle_ex", "handle_docs", "handle_index", "handle_lesson")
            )
            else MagicMock()
        )
        with patch.object(session, target, mock):
            assert await session.handle_command(cmd) is True
        mock.assert_called_once()

    async def test_exit_and_quit(self, session: REPLSession) -> None:
        assert await session.handle_command("/exit") is False
        assert await session.handle_command("/QUIT") is False

    async def test_clear_and_reset_with_agent(self, session: REPLSession) -> None:
        session.chat_agent = MagicMock()
        with patch.object(repl_mod, "console"), patch.object(repl_mod, "print_info"):
            await session.handle_command("/clear")
            await session.handle_command("/reset")
        assert session.chat_agent.reset_conversation.call_count == 2

    async def test_clear_and_reset_without_agent(self, session: REPLSession) -> None:
        session.chat_agent = None
        with patch.object(repl_mod, "console"), patch.object(repl_mod, "print_info"):
            assert await session.handle_command("/clear") is True
            assert await session.handle_command("/reset") is True

    async def test_unknown(self, session: REPLSession) -> None:
        with patch.object(repl_mod, "print_error") as err:
            assert await session.handle_command("/nope") is True
        assert "Unknown command" in err.call_args[0][0]


class TestDisplays:
    def test_help_history_agents(self, session: REPLSession) -> None:
        with patch.object(repl_mod, "console") as con, patch.object(repl_mod, "print_info") as info:
            session.show_help()
            session.show_history()
            assert "No messages" in info.call_args[0][0]
            session.session.add_message("user", "hi")
            session.session.add_message("assistant", "x" * 300)
            session.show_history()
            session.agents = {
                "a": SimpleNamespace(config=SimpleNamespace(description="d", model="m"))
            }
            session.show_agents()
        assert con.print.called


class TestSkills:
    def _skill(self, name: str = "s1", model: str | None = None, refs: list | None = None) -> Any:
        return SimpleNamespace(
            name=name,
            description="d" * 80,
            references=refs or [],
            model=model,
            content="body",
        )

    def test_list_empty(self, session: REPLSession) -> None:
        session.skill_loader = MagicMock(list_all=MagicMock(return_value={}))
        with patch.object(repl_mod, "print_info") as info:
            session.handle_skill_command("")
        assert "No skills" in info.call_args[0][0]

    def test_list_with_active(self, session: REPLSession) -> None:
        session.skill_loader = MagicMock(
            list_all=MagicMock(
                return_value={"s1": self._skill(refs=["a"]), "s2": self._skill("s2", "m")}
            )
        )
        session.active_skill = "s1"
        with patch.object(repl_mod, "console") as con:
            session.handle_skill_command("")
        assert any("Active skill" in str(c) for c in con.print.call_args_list)

    def test_off(self, session: REPLSession) -> None:
        session.chat_agent = MagicMock()
        session.active_skill = "s"
        with patch.object(repl_mod, "print_info"):
            session.handle_skill_command("off")
        session.chat_agent.deactivate_skill.assert_called_once()
        assert session.active_skill is None
        with patch.object(repl_mod, "print_info"):
            session.handle_skill_command("off")  # no active skill

    def test_chain_usage_missing_and_ok(self, session: REPLSession) -> None:
        loader = MagicMock()
        session.skill_loader = loader
        with patch.object(repl_mod, "print_error") as err:
            session.handle_skill_command("chain")
            assert "Usage" in err.call_args[0][0]
            loader.get_chain.return_value = []
            session.handle_skill_command("chain nope")
            assert "not found" in err.call_args[0][0]
        loader.get_chain.return_value = [self._skill("a", "m"), self._skill("b")]
        session.chat_agent = MagicMock()
        with patch.object(repl_mod, "print_success") as ok:
            session.handle_skill_command("chain a")
        assert session.active_skill == "a" and "(model: m)" in ok.call_args[0][0]
        loader.get_chain.return_value = [self._skill("a")]
        session.chat_agent = None
        with patch.object(repl_mod, "print_success") as ok:
            session.handle_skill_command("chain a")
        assert "(model" not in ok.call_args[0][0]

    def test_single_skill(self, session: REPLSession) -> None:
        loader = MagicMock()
        session.skill_loader = loader
        loader.get.return_value = None
        with patch.object(repl_mod, "print_error"), patch.object(repl_mod, "print_info"):
            session.handle_skill_command("ghost")
        loader.get.return_value = self._skill("s1", "m")
        session.chat_agent = MagicMock()
        with patch.object(repl_mod, "print_success") as ok:
            session.handle_skill_command("s1 extra ctx")
        args = session.chat_agent.activate_skill.call_args
        assert "Additional Context" in args[0][1] and "(model: m)" in ok.call_args[0][0]
        loader.get.return_value = self._skill("s1")
        session.chat_agent = None
        with patch.object(repl_mod, "print_success"):
            session.handle_skill_command("s1")
        assert session.active_skill == "s1"


class TestConfigCommand:
    def test_summary_with_embedding_url(self, session: REPLSession) -> None:
        session.settings.ollama.embedding_api_url = "http://emb:11434"
        with patch.object(repl_mod, "console") as con:
            session.handle_config_command("")
        assert any("Embedding URL" in str(c) for c in con.print.call_args_list)

    def test_summary_without_embedding_url(self, session: REPLSession) -> None:
        session.settings.ollama.embedding_api_url = ""
        with patch.object(repl_mod, "console") as con:
            session.handle_config_command("")
        assert not any("Embedding URL" in str(c) for c in con.print.call_args_list)

    def test_show_masks_secrets(self, session: REPLSession) -> None:
        data = {
            "a": {
                "api_key": "s3cret",
                "b": [{"jwt_token": "t", "ok": 1}],
                "empty": "",
                "api_key2": 1,
            }
        }
        with (
            patch.object(repl_mod, "settings_to_dict", return_value=data),
            patch.object(repl_mod, "console") as con,
        ):
            session.handle_config_command("show")
        rendered = " ".join(str(c) for c in con.print.call_args_list)
        assert "****" in rendered and "s3cret" not in rendered

    def test_reset_ok_and_fail(self, session: REPLSession) -> None:
        session.chat_agent = MagicMock()
        new = Settings()
        with (
            patch.object(repl_mod, "load_settings", return_value=new),
            patch.object(repl_mod, "print_success"),
        ):
            session.handle_config_command("reset")
        assert session.settings is new and session.chat_agent.settings is new
        session.chat_agent = None
        with (
            patch.object(repl_mod, "load_settings", return_value=new),
            patch.object(repl_mod, "print_success"),
        ):
            session.handle_config_command("reset")
        with (
            patch.object(repl_mod, "load_settings", side_effect=OSError("x")),
            patch.object(repl_mod, "print_error") as err,
        ):
            session.handle_config_command("reset")
        assert "Failed to reload" in err.call_args[0][0]

    def test_save_ok_and_fail(self, session: REPLSession) -> None:
        with (
            patch.object(repl_mod, "save_settings", return_value="/p"),
            patch.object(repl_mod, "print_success") as ok,
        ):
            session.handle_config_command("save")
        assert "/p" in ok.call_args[0][0]
        with (
            patch.object(repl_mod, "save_settings", side_effect=OSError("x")),
            patch.object(repl_mod, "print_error") as err,
        ):
            session.handle_config_command("save")
        assert "Failed to save" in err.call_args[0][0]

    def test_set_variants(self, session: REPLSession) -> None:
        session.chat_agent = MagicMock()
        with patch.object(repl_mod, "print_error") as err:
            session.handle_config_command("set onlykey")
            assert "Usage" in err.call_args[0][0]
        with (
            patch.object(repl_mod, "set_config_value", return_value=("a", "b")),
            patch.object(repl_mod, "print_success"),
        ):
            session.handle_config_command("set models.orchestration b")
            assert session.chat_agent.model == "b"
        with (
            patch.object(repl_mod, "set_config_value", return_value=(1, 4)),
            patch.object(repl_mod, "print_success"),
        ):
            session.handle_config_command("set regulators.max_concurrent_agents 4")
            session.chat_agent.agent_semaphore.adjust_max.assert_called_once_with(4)
        with (
            patch.object(repl_mod, "set_config_value", return_value=(1, 2)),
            patch.object(repl_mod, "print_success"),
        ):
            session.handle_config_command("set other.key 2")
        session.chat_agent = None
        with (
            patch.object(repl_mod, "set_config_value", return_value=(1, 2)),
            patch.object(repl_mod, "print_success"),
        ):
            session.handle_config_command("set models.orchestration x")
            session.handle_config_command("set regulators.max_concurrent_agents 2")
        with (
            patch.object(repl_mod, "set_config_value", side_effect=AttributeError),
            patch.object(repl_mod, "print_error") as err,
        ):
            session.handle_config_command("set bad.key 1")
        assert "Unknown config key" in err.call_args[0][0]
        with (
            patch.object(repl_mod, "set_config_value", side_effect=ValueError("nope")),
            patch.object(repl_mod, "print_error") as err,
        ):
            session.handle_config_command("set models.x 1")
        assert "Invalid value" in err.call_args[0][0]

    def test_dotpath_lookup(self, session: REPLSession) -> None:
        with patch.object(repl_mod, "console") as con:
            session.handle_config_command("models.execution")
        assert con.print.called
        with patch.object(repl_mod, "print_error") as err:
            session.handle_config_command("no.such.key")
        assert "Unknown config key" in err.call_args[0][0]


class TestAgentCommands:
    async def test_read(self, session: REPLSession) -> None:
        explorer = MagicMock()
        session.agents = {"explorer": explorer}
        with patch.object(repl_mod, "print_error") as err:
            await session.handle_read("")
            assert "Usage" in err.call_args[0][0]
            explorer.execute_tool = AsyncMock(
                return_value=SimpleNamespace(success=False, error=None, data=None)
            )
            await session.handle_read("f")
            assert "Failed to read" in err.call_args[0][0]
        explorer.execute_tool = AsyncMock(
            return_value=SimpleNamespace(success=True, data="file body")
        )
        with patch.object(repl_mod, "console") as con:
            await session.handle_read("f")
        con.print.assert_called_with("file body")

    async def test_explore(self, session: REPLSession) -> None:
        explorer = MagicMock()
        session.agents = {"explorer": explorer}
        with patch.object(repl_mod, "print_error") as err, patch.object(repl_mod, "console"):
            await session.handle_explore("")
            assert "Usage" in err.call_args[0][0]
            explorer.run = AsyncMock(
                return_value=SimpleNamespace(success=False, error="boom", output="")
            )
            await session.handle_explore("q")
            assert err.call_args[0][0] == "boom"
        explorer.run = AsyncMock(return_value=SimpleNamespace(success=True, output="found"))
        with patch.object(repl_mod, "console") as con:
            await session.handle_explore("q")
        con.print.assert_called_with("found")

    async def test_execute(self, session: REPLSession) -> None:
        executor = MagicMock()
        session.agents = {"executor": executor}
        with patch.object(repl_mod, "print_error") as err, patch.object(repl_mod, "console"):
            await session.handle_execute("")
            assert "Usage" in err.call_args[0][0]
            executor.run = AsyncMock(
                return_value=SimpleNamespace(success=False, error=None, output="")
            )
            await session.handle_execute("t")
            assert "Execution failed" in err.call_args[0][0]
        executor.run = AsyncMock(return_value=SimpleNamespace(success=True, output="ok"))
        with patch.object(repl_mod, "console"), patch.object(repl_mod, "print_success") as ok:
            await session.handle_execute("t")
        assert "completed" in ok.call_args[0][0]


class TestIndexCodeResults:
    async def test_no_wait_queued_wait_job_and_no_job(
        self, session: REPLSession, tmp_path: Path
    ) -> None:
        session.knowledge_client = MagicMock()
        ic = AsyncMock(return_value=SimpleNamespace(job_id="j1", node_count=2, edge_count=3))
        session.knowledge_client.index_code = ic
        with patch.object(repl_mod, "console") as con, patch.object(repl_mod, "print_info") as info:
            await session.handle_index_code(f"{tmp_path} --no-wait")
        assert "queued (job j1)" in info.call_args[0][0]
        with (
            patch.object(repl_mod, "console") as con,
            patch.object(repl_mod, "print_success") as ok,
        ):
            await session.handle_index_code("")
        assert any("Job j1" in str(c) for c in con.print.call_args_list)
        assert "2 node(s), 3 edge(s)" in ok.call_args[0][0]
        ic.return_value = SimpleNamespace(job_id="", node_count=1, edge_count=0)
        with patch.object(repl_mod, "console") as con, patch.object(repl_mod, "print_success"):
            await session.handle_index_code("")
        assert not any("Job" in str(c) for c in con.print.call_args_list)


class TestDocsCommand:
    async def test_disabled(self, session: REPLSession) -> None:
        session.settings.docs_rag.enabled = False
        with patch.object(repl_mod, "print_error") as err:
            await session.handle_docs_command("status")
        assert "disabled" in err.call_args[0][0]

    @pytest.mark.parametrize(
        ("args", "target", "arg"),
        [
            ("", "_docs_status", None),
            ("detect", "_docs_detect", None),
            ("index lib", "_docs_index", "lib"),
            ("search q", "_docs_search", "q"),
            ("clear lib", "_docs_clear", "lib"),
            ("cleanup", "_docs_cleanup", None),
        ],
    )
    async def test_routes(
        self, session: REPLSession, args: str, target: str, arg: str | None
    ) -> None:
        mock = AsyncMock()
        with patch.object(session, target, mock):
            await session.handle_docs_command(args)
        mock.assert_awaited_once()
        if arg is not None:
            mock.assert_awaited_once_with(arg)

    async def test_unknown(self, session: REPLSession) -> None:
        with patch.object(repl_mod, "print_error") as err, patch.object(repl_mod, "print_info"):
            await session.handle_docs_command("zzz")
        assert "Unknown docs command" in err.call_args[0][0]


class TestDocsStatus:
    async def test_full_status(self, session: REPLSession) -> None:
        libs = [_lib(f"l{i}") for i in range(12)] + [_lib("g", Language.GO)]
        session.project_context = ProjectContext(
            languages=[Language.PYTHON, Language.GO], libraries=libs
        )
        lib_status = SimpleNamespace(language="python", chunk_count=3, is_expired=True)
        lang_status = SimpleNamespace(chunk_count=5, is_expired=False)
        session.knowledge_client = MagicMock(
            index_status=AsyncMock(
                return_value=SimpleNamespace(
                    libraries={"l1": lib_status}, languages={"python": lang_status}, total_chunks=8
                )
            )
        )
        session.docs_fetcher = MagicMock(
            get_cache_stats=MagicMock(return_value={"valid_entries": 1, "expired_entries": 2})
        )
        with patch.object(repl_mod, "console") as con:
            await session._docs_status()
        out = " ".join(str(c) for c in con.print.call_args_list)
        assert "and 2 more" in out and "Total chunks: 8" in out and "Valid entries: 1" in out

    async def test_no_context_nothing_indexed_and_error(self, session: REPLSession) -> None:
        session.project_context = None
        session.docs_fetcher = None
        session.knowledge_client = MagicMock(
            index_status=AsyncMock(
                return_value=SimpleNamespace(libraries={}, languages={}, total_chunks=0)
            )
        )
        with patch.object(repl_mod, "console"), patch.object(repl_mod, "print_info") as info:
            await session._docs_status()
        assert any("Nothing indexed" in c[0][0] for c in info.call_args_list)
        session.knowledge_client.index_status = AsyncMock(side_effect=KnowledgeClientError("x"))
        with (
            patch.object(repl_mod, "console"),
            patch.object(repl_mod, "print_info"),
            patch.object(repl_mod, "print_error") as err,
        ):
            await session._docs_status()
        assert "Failed to fetch index status" in err.call_args[0][0]

    async def test_no_client(self, session: REPLSession) -> None:
        session.knowledge_client = None
        with patch.object(repl_mod, "console"), patch.object(repl_mod, "print_info"):
            await session._docs_status()


class TestDocsDetect:
    async def test_with_and_without_languages(self, session: REPLSession) -> None:
        many = [_lib(f"l{i}", ver=("1" if i % 2 else None)) for i in range(17)]
        with (
            patch("penguincode_cli.docs_rag.ProjectDetector") as det,
            patch.object(repl_mod, "console") as con,
            patch.object(repl_mod, "print_info") as info,
        ):
            det.return_value.detect.return_value = ProjectContext(
                languages=[Language.PYTHON], libraries=many
            )
            await session._docs_detect()
            assert any("2 more" in str(c) for c in con.print.call_args_list)
            det.return_value.detect.return_value = ProjectContext(
                languages=[Language.PYTHON], libraries=many[:2]
            )
            await session._docs_detect()
            det.return_value.detect.return_value = ProjectContext()
            await session._docs_detect()
        assert "No languages" in info.call_args[0][0]


class TestDocsIndex:
    async def test_guards(self, session: REPLSession) -> None:
        with patch.object(repl_mod, "print_error") as err:
            await session._docs_index()
            assert "detect first" in err.call_args[0][0]
            session.project_context = ProjectContext()
            session.knowledge_client = None
            await session._docs_index()
            assert "Not connected" in err.call_args[0][0]

    async def test_named_library(self, session: REPLSession) -> None:
        session.project_context = ProjectContext(
            languages=[Language.PYTHON], libraries=[_lib("Req", ver=None)]
        )
        session.docs_fetcher = MagicMock(fetch_library_docs=AsyncMock(return_value=["doc"]))
        session.knowledge_client = MagicMock(
            index=AsyncMock(return_value=SimpleNamespace(chunks_indexed=3))
        )
        with patch.object(repl_mod, "console"), patch.object(repl_mod, "print_success") as ok:
            await session._docs_index("req")
        assert session.knowledge_client.index.call_args.kwargs["library_version"] == ""
        assert "3 total" in ok.call_args[0][0]
        with patch.object(repl_mod, "print_error") as err:
            await session._docs_index("ghost")
        assert "not detected" in err.call_args[0][0]

    async def test_priority_libs_error_and_no_docs(self, session: REPLSession) -> None:
        libs = [_lib("a"), _lib("b")]
        session.project_context = ProjectContext(languages=[Language.PYTHON], libraries=libs)
        session.docs_fetcher = MagicMock(fetch_library_docs=AsyncMock(side_effect=[["d"], []]))
        session.knowledge_client = MagicMock(index=AsyncMock(side_effect=KnowledgeClientError("x")))
        with (
            patch("penguincode_cli.docs_rag.get_priority_docs_for_project", return_value=libs),
            patch.object(repl_mod, "console") as con,
            patch.object(repl_mod, "print_success"),
        ):
            await session._docs_index()
        out = " ".join(str(c) for c in con.print.call_args_list)
        assert "Failed" in out and "No docs found" in out


class TestDocsSearchBranches:
    def _s(self, session: REPLSession, **client: Any) -> REPLSession:
        session.project_context = None
        session.knowledge_client = MagicMock(
            current_token_for_cache_scoping=AsyncMock(return_value=client.pop("token", "tok")),
            **client,
        )
        return session

    async def test_guards(self, session: REPLSession) -> None:
        with patch.object(repl_mod, "print_error") as err:
            await session._docs_search("")
            assert "Usage" in err.call_args[0][0]
            session.knowledge_client = None
            await session._docs_search("q")
            assert "Not connected" in err.call_args[0][0]

    async def test_live_hits_filtered_truncated_and_cached(self, session: REPLSession) -> None:
        hit = lambda lib, doc: SimpleNamespace(document=doc, metadata={"library": lib}, score=0.5)  # noqa: E731
        s = self._s(
            session,
            query=AsyncMock(
                return_value=SimpleNamespace(vector_hits=[hit("Req", "x" * 400), hit("other", "y")])
            ),
        )
        s.project_context = ProjectContext(libraries=[_lib("req")])
        s.offline_cache = MagicMock()
        with patch.object(repl_mod, "console") as con:
            await s._docs_search("q")
        out = " ".join(str(c) for c in con.print.call_args_list)
        assert "..." in out and "other" not in out
        s.offline_cache.set.assert_called_once()

    async def test_no_token_no_hits(self, session: REPLSession) -> None:
        s = self._s(
            session, token=None, query=AsyncMock(return_value=SimpleNamespace(vector_hits=[]))
        )
        s.offline_cache = MagicMock()
        with patch.object(repl_mod, "console"), patch.object(repl_mod, "print_info") as info:
            await s._docs_search("q")
        s.offline_cache.set.assert_not_called()
        assert "No results" in info.call_args[0][0]

    async def test_unavailable_without_token_has_no_cache(self, session: REPLSession) -> None:
        s = self._s(
            session,
            token=None,
            query=AsyncMock(side_effect=KnowledgeServerUnavailableError("down")),
        )
        with patch.object(repl_mod, "console"), patch.object(repl_mod, "print_error") as err:
            await s._docs_search("q")
        assert "no cached result" in err.call_args[0][0]

    @pytest.mark.parametrize(("stale", "word"), [(True, "stale"), (False, "cached")])
    async def test_unavailable_serves_cache(
        self, session: REPLSession, stale: bool, word: str
    ) -> None:
        s = self._s(session, query=AsyncMock(side_effect=KnowledgeServerUnavailableError("down")))
        cached = SimpleNamespace(
            value={"hits": [{"document": "d", "metadata": {"library": "l"}, "score": 1.0}]},
            age_seconds=lambda: 600,
            is_stale=stale,
        )
        s.offline_cache = MagicMock(get=MagicMock(return_value=cached))
        with patch.object(repl_mod, "console") as con, patch.object(repl_mod, "print_error"):
            await s._docs_search("q")
        assert any(word in str(c) and "10 min" in str(c) for c in con.print.call_args_list)

    async def test_generic_client_error(self, session: REPLSession) -> None:
        s = self._s(session, query=AsyncMock(side_effect=KnowledgeClientError("bad")))
        with patch.object(repl_mod, "console"), patch.object(repl_mod, "print_error") as err:
            await s._docs_search("q")
        assert "Search failed: bad" in err.call_args[0][0]


class TestDocsClearCleanup:
    async def test_clear_guards_and_results(self, session: REPLSession) -> None:
        with patch.object(repl_mod, "print_error") as err:
            session.knowledge_client = None
            await session._docs_clear("l")
            assert "Not connected" in err.call_args[0][0]
            session.knowledge_client = MagicMock(clear_index=AsyncMock())
            await session._docs_clear("")
            assert "Usage" in err.call_args[0][0]
            session.knowledge_client.clear_index = AsyncMock(side_effect=KnowledgeClientError("x"))
            await session._docs_clear("l")
            assert "Failed to clear" in err.call_args[0][0]
        session.knowledge_client.clear_index = AsyncMock(return_value=2)
        with patch.object(repl_mod, "print_success") as ok:
            await session._docs_clear("l")
        assert "Cleared 2" in ok.call_args[0][0]
        session.knowledge_client.clear_index = AsyncMock(return_value=0)
        with patch.object(repl_mod, "print_info") as info:
            await session._docs_clear("l")
        assert "Nothing indexed" in info.call_args[0][0]

    async def test_cleanup_no_context(self, session: REPLSession) -> None:
        with patch.object(repl_mod, "print_error") as err:
            await session._docs_cleanup()
        assert "detect first" in err.call_args[0][0]

    async def test_cleanup_both_error_and_nothing(self, session: REPLSession) -> None:
        session.project_context = ProjectContext(languages=[Language.PYTHON], libraries=[_lib()])
        session.docs_fetcher = MagicMock(cleanup_unused_libraries=MagicMock(return_value={"x": 2}))
        session.knowledge_client = MagicMock(cleanup_index=AsyncMock(return_value={"y": 4}))
        with patch.object(repl_mod, "console") as con:
            await session._docs_cleanup()
        out = " ".join(str(c) for c in con.print.call_args_list)
        assert "Cache: removed 2" in out and "Index: removed 4" in out
        session.knowledge_client.cleanup_index = AsyncMock(side_effect=KnowledgeClientError("x"))
        session.docs_fetcher.cleanup_unused_libraries.return_value = {}
        with (
            patch.object(repl_mod, "print_error") as err,
            patch.object(repl_mod, "print_info") as info,
        ):
            await session._docs_cleanup()
        assert (
            "server-side index" in err.call_args[0][0]
            and "Nothing to clean" in info.call_args[0][0]
        )
        session.knowledge_client = None
        with patch.object(repl_mod, "print_info") as info:
            await session._docs_cleanup()
        assert "Nothing to clean" in info.call_args[0][0]


class TestDetectLanguages:
    @pytest.mark.parametrize(
        ("msg", "expected"),
        [
            ("fix my pytest suite", "python"),
            ("add a react component", "javascript"),
            ("rename the .tsx file", "typescript"),
            ("write a golang service", "go"),
            ("cargo build fails", "rust"),
            ("terraform plan", "hcl"),
            ("run the playbook", "ansible"),
            ("rails migration", "ruby"),
            ("laravel route", "php"),
            ("flutter widget", "dart"),
        ],
    )
    def test_detects(self, session: REPLSession, msg: str, expected: str) -> None:
        assert expected in session._detect_languages_in_message(msg)

    def test_none(self, session: REPLSession) -> None:
        assert session._detect_languages_in_message("hello there") == []


class TestHandleChat:
    def _chat_session(self, session: REPLSession) -> REPLSession:
        session.chat_agent = MagicMock(
            system_prompt="base", process=AsyncMock(return_value="reply")
        )
        session.settings.docs_rag.auto_detect_on_request = False
        return session

    async def test_plain_reply_saved(self, session: REPLSession) -> None:
        s = self._chat_session(session)
        with patch.object(repl_mod, "console"):
            await s.handle_chat("hi")
        assert [m.role for m in s.session.messages] == ["user", "assistant"]

    async def test_empty_reply_not_saved_and_original_prompt_restored(
        self, session: REPLSession
    ) -> None:
        s = self._chat_session(session)
        s.chat_agent.process = AsyncMock(return_value="")
        s._original_prompt = "orig"
        with patch.object(repl_mod, "console"):
            await s.handle_chat("hi")
        assert [m.role for m in s.session.messages] == ["user"]
        assert s.chat_agent.system_prompt == "orig"

    async def test_on_demand_indexing_and_context_injection(self, session: REPLSession) -> None:
        s = self._chat_session(session)
        s.settings.docs_rag.auto_detect_on_request = True
        s.settings.docs_rag.auto_index_on_request = True
        s.project_context = ProjectContext()
        s.context_injector = MagicMock(
            should_inject_context=AsyncMock(return_value=True),
            get_relevant_context=AsyncMock(return_value="ctx"),
            build_augmented_prompt=MagicMock(return_value="augmented"),
        )
        with (
            patch.object(s, "_ensure_language_indexed", AsyncMock()) as ensure,
            patch.object(repl_mod, "console") as con,
        ):
            await s.handle_chat("my python code")
        ensure.assert_awaited_once_with("python")
        assert any("documentation context" in str(c) for c in con.print.call_args_list)

    async def test_injector_declines_or_empty_context(self, session: REPLSession) -> None:
        s = self._chat_session(session)
        s.project_context = ProjectContext()
        s.context_injector = MagicMock(
            should_inject_context=AsyncMock(return_value=False),
            get_relevant_context=AsyncMock(return_value=""),
        )
        with patch.object(repl_mod, "console"):
            await s.handle_chat("hi")
            s.context_injector.should_inject_context = AsyncMock(return_value=True)
            await s.handle_chat("hi")
        assert s.chat_agent.system_prompt == "base"

    async def test_agent_error_is_reported_not_raised(self, session: REPLSession) -> None:
        s = self._chat_session(session)
        s.chat_agent.process = AsyncMock(side_effect=RuntimeError("model down"))
        with patch.object(repl_mod, "console") as con:
            await s.handle_chat("hi")
        assert any("model down" in str(c) for c in con.print.call_args_list)


class _FakePrompt:
    """`PromptSession` stand-in feeding scripted inputs / exceptions to `run()`."""

    script: list[Any] = []

    def __init__(self, **_: Any) -> None:
        pass

    def prompt(self, _text: str) -> str:
        item = _FakePrompt.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class TestRunLoop:
    async def _run(self, session: REPLSession, script: list[Any]) -> None:
        _FakePrompt.script = list(script)
        session.handle_chat = AsyncMock()  # type: ignore[method-assign]
        with (
            patch.object(repl_mod, "PromptSession", _FakePrompt),
            patch.object(repl_mod, "console"),
            patch.object(repl_mod, "print_error") as err,
            patch.object(repl_mod, "print_info"),
            patch.object(session, "_check_for_update", AsyncMock()),
        ):
            await session.run()
            self.err = err

    async def test_chat_command_blank_exit(self, session: REPLSession) -> None:
        session.active_skill = "sk"
        await self._run(session, ["hello", "   ", "/help", "/exit"])
        session.handle_chat.assert_awaited_once_with("hello")
        assert session._update_check_task is not None
        await session._update_check_task

    async def test_eof_exits(self, session: REPLSession) -> None:
        await self._run(session, [EOFError()])

    async def test_double_ctrl_c_exits(self, session: REPLSession) -> None:
        await self._run(session, [KeyboardInterrupt(), KeyboardInterrupt()])

    async def test_single_ctrl_c_then_input_resets(self, session: REPLSession) -> None:
        await self._run(session, [KeyboardInterrupt(), "x", KeyboardInterrupt(), "/exit"])
        session.handle_chat.assert_awaited_once_with("x")

    async def test_interrupt_flavoured_exception(self, session: REPLSession) -> None:
        await self._run(session, [RuntimeError("User interrupt"), RuntimeError("interrupt again")])

    async def test_interrupt_flavoured_then_recover(self, session: REPLSession) -> None:
        await self._run(session, [RuntimeError("interrupt"), "/exit"])

    async def test_other_exception_reported_and_loop_continues(self, session: REPLSession) -> None:
        await self._run(session, [RuntimeError("kaput"), "/exit"])
        assert "kaput" in self.err.call_args[0][0]


class TestStartRepl:
    async def test_start_repl_runs_inside_context(self) -> None:
        inst = MagicMock()
        inst.__aenter__ = AsyncMock(return_value=inst)
        inst.__aexit__ = AsyncMock(return_value=False)
        inst.run = AsyncMock()
        with patch.object(repl_mod, "REPLSession", return_value=inst) as cls:
            await repl_mod.start_repl("proj", "cfg.yaml")
        cls.assert_called_once_with("proj", "cfg.yaml")
        inst.run.assert_awaited_once()
