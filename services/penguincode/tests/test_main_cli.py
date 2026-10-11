"""Coverage for the `penguincode` Typer CLI entry points in `penguincode_cli.main`.

Every external effect (the REPL, the gRPC server, bootstrap, subprocess, the live web) is
patched at the boundary the command calls -- no real network, no real `ollama`/`pip`/`code`
processes. `HOME` is redirected to `tmp_path` so `Path.home()` writes never touch the real one.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from penguincode_cli import main as cli

runner = CliRunner()


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME at a scratch dir so `Path.home()` never resolves to the real one."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _close_coro(coro: object) -> None:
    """Stand-in for `asyncio.run` that disposes of the coroutine it is handed."""
    close = getattr(coro, "close", None)
    if close:
        close()


class TestChat:
    def test_plain_chat_runs_repl(self) -> None:
        with patch.object(cli.asyncio, "run", side_effect=_close_coro) as run:
            result = runner.invoke(cli.app, ["chat", "-p", ".", "-c", "x.yaml"])
        assert result.exit_code == 0
        assert run.call_count == 1

    def test_debug_enables_debug_logging(self) -> None:
        with (
            patch.object(cli.asyncio, "run", side_effect=_close_coro),
            patch("penguincode_cli.core.debug.enable_debug") as enable,
        ):
            result = runner.invoke(cli.app, ["chat", "--debug"])
        assert result.exit_code == 0
        enable.assert_called_once()
        assert "Debug mode enabled" in result.output


class TestServe:
    def test_serve_runs_server(self) -> None:
        with (
            patch.object(cli.asyncio, "run", side_effect=_close_coro) as run,
            patch("logging.basicConfig") as basic,
        ):
            result = runner.invoke(cli.app, ["serve", "--port", "1", "--rest-port", "2"])
        assert result.exit_code == 0
        basic.assert_called_once()
        assert run.call_count == 1


class TestLaunch:
    def test_no_exec_prints_provisioned_summary(self) -> None:
        cfg = {"license": {"tier": "pro"}, "agents": {"a": 1}}
        with patch.object(cli.asyncio, "run", return_value=cfg) as run:
            result = runner.invoke(cli.app, ["launch", "--no-exec"])
        assert result.exit_code == 0
        assert "tier=pro" in result.output and "agents=['a']" in result.output
        # bootstrap() coroutine was built but asyncio.run is a stub: close it.
        _close_coro(run.call_args[0][0])

    def test_exec_mode_prints_no_summary(self) -> None:
        with patch.object(cli.asyncio, "run", return_value={}) as run:
            result = runner.invoke(cli.app, ["launch"])
        assert result.exit_code == 0
        assert "Provisioned" not in result.output
        _close_coro(run.call_args[0][0])

    def test_runtime_error_exits_1(self) -> None:
        with patch.object(cli.asyncio, "run", side_effect=RuntimeError("boom")):
            with patch("penguincode_cli.client.bootstrap.bootstrap", new=MagicMock()):
                result = runner.invoke(cli.app, ["launch"])
        assert result.exit_code == 1
        assert "Launch failed" in result.output


class TestConfig:
    def test_show_renders_tables(self) -> None:
        settings = SimpleNamespace(
            ollama=SimpleNamespace(api_url="http://o", embedding_api_url="", timeout=5),
            models=SimpleNamespace(planning="p", orchestration="o", research="r", execution="e"),
        )
        with patch.object(cli, "load_settings", return_value=settings):
            result = runner.invoke(cli.app, ["config", "show"])
        assert result.exit_code == 0
        assert "unset" in result.output  # embedding-config row falls back to API URL

    def test_show_embedding_url_set(self) -> None:
        settings = SimpleNamespace(
            ollama=SimpleNamespace(api_url="http://o", embedding_api_url="http://emb", timeout=5),
            models=SimpleNamespace(planning="p", orchestration="o", research="r", execution="e"),
        )
        with patch.object(cli, "load_settings", return_value=settings):
            result = runner.invoke(cli.app, ["config"])
        assert result.exit_code == 0
        assert "http://emb" in result.output

    def test_show_missing_file(self) -> None:
        with patch.object(cli, "load_settings", side_effect=FileNotFoundError):
            result = runner.invoke(cli.app, ["config", "show"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_show_other_error(self) -> None:
        with patch.object(cli, "load_settings", side_effect=ValueError("bad")):
            result = runner.invoke(cli.app, ["config", "show"])
        assert result.exit_code == 1
        assert "Error loading config" in result.output

    def test_set_requires_key_and_value(self) -> None:
        result = runner.invoke(cli.app, ["config", "set"])
        assert result.exit_code == 1
        assert "required" in result.output

    def test_set_with_key_and_value(self) -> None:
        result = runner.invoke(cli.app, ["config", "set", "-k", "a.b", "-v", "1"])
        assert result.exit_code == 0
        assert "not fully implemented" in result.output

    def test_unknown_action(self) -> None:
        result = runner.invoke(cli.app, ["config", "nope"])
        assert result.exit_code == 1
        assert "Unknown action" in result.output


class TestHistory:
    def test_no_sessions(self) -> None:
        mgr = MagicMock()
        mgr.list_sessions.return_value = []
        with patch.object(cli, "SessionManager", return_value=mgr):
            result = runner.invoke(cli.app, ["history"])
        assert result.exit_code == 0
        assert "No sessions found" in result.output

    def test_lists_sessions(self) -> None:
        mgr = MagicMock()
        mgr.list_sessions.return_value = [
            {"session_id": "sid-1", "created_at": "2026-01-01", "message_count": 3}
        ]
        with patch.object(cli, "SessionManager", return_value=mgr):
            result = runner.invoke(cli.app, ["history", "-l", "1"])
        assert result.exit_code == 0
        assert "sid-1" in result.output

    def test_error_exits_1(self) -> None:
        with patch.object(cli, "SessionManager", side_effect=OSError("denied")):
            result = runner.invoke(cli.app, ["history"])
        assert result.exit_code == 1
        assert "Error loading history" in result.output


def _resp(status: int = 200, models: list | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = {"models": models if models is not None else []}
    return r


def _proc(rc: int = 0, err: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=rc, stderr=err)


class TestSetup:
    BASE = ["setup", "--skip-ollama-check", "--no-pull-models"]

    def test_not_in_venv_skips_deps(self) -> None:
        with patch.object(sys, "prefix", "/a"), patch.object(sys, "base_prefix", "/a"):
            result = runner.invoke(cli.app, self.BASE)
        assert result.exit_code == 0
        assert "Not running inside a virtual environment" in result.output

    @pytest.mark.parametrize(
        ("run_effect", "needle"),
        [
            (_proc(0), "Python dependencies installed"),
            (_proc(1, "err text"), "Dependency install warning"),
            (subprocess.TimeoutExpired("pip", 1), "timed out"),
            (RuntimeError("x"), "Could not install dependencies"),
        ],
    )
    def test_in_venv_install_outcomes(self, run_effect: object, needle: str) -> None:
        kw = (
            {"side_effect": run_effect}
            if isinstance(run_effect, BaseException)
            else {"return_value": run_effect}
        )
        with (
            patch.object(sys, "prefix", "/venv"),
            patch.object(sys, "base_prefix", "/base"),
            patch.object(cli.subprocess, "run", **kw),
        ):
            result = runner.invoke(cli.app, self.BASE)
        assert result.exit_code == 0
        assert needle in result.output

    def test_ollama_running_with_models(self) -> None:
        with patch.object(cli.httpx, "get", return_value=_resp(200, [{"n": 1}])):
            result = runner.invoke(cli.app, ["setup", "--skip-deps", "--no-pull-models"])
        assert result.exit_code == 0
        assert "Found 1 model" in result.output

    def test_ollama_running_no_models(self) -> None:
        with patch.object(cli.httpx, "get", return_value=_resp(200, [])):
            result = runner.invoke(cli.app, ["setup", "--skip-deps", "--no-pull-models"])
        assert "Ollama is running" in result.output
        assert "Found" not in result.output

    def test_ollama_bad_status(self) -> None:
        with patch.object(cli.httpx, "get", return_value=_resp(503)):
            result = runner.invoke(cli.app, ["setup", "--skip-deps", "--no-pull-models"])
        assert "status 503" in result.output

    def test_connect_error_without_pull_exits(self) -> None:
        with patch.object(cli.httpx, "get", side_effect=httpx.ConnectError("no")):
            result = runner.invoke(cli.app, ["setup", "--skip-deps", "--no-pull-models"])
        assert result.exit_code == 1
        assert "Cannot connect to Ollama" in result.output

    def test_connect_error_with_pull_skips_models(self) -> None:
        with (
            patch.object(cli.httpx, "get", side_effect=httpx.ConnectError("no")),
            patch.object(cli.subprocess, "run") as run,
        ):
            result = runner.invoke(cli.app, ["setup", "--skip-deps"])
        assert result.exit_code == 0
        assert "Skipping model pull" in result.output
        run.assert_not_called()

    def test_other_ollama_error_skips_pull(self) -> None:
        with (
            patch.object(cli.httpx, "get", side_effect=RuntimeError("weird")),
            patch.object(cli.subprocess, "run") as run,
        ):
            result = runner.invoke(cli.app, ["setup", "--skip-deps"])
        assert "Error checking Ollama" in result.output
        run.assert_not_called()

    def test_pull_models_all_outcomes(self) -> None:
        effects = [
            _proc(0),
            _proc(1, "nope"),
            subprocess.TimeoutExpired("ollama", 1),
            RuntimeError("kaboom"),
        ]
        with patch.object(cli.subprocess, "run", side_effect=effects) as run:
            result = runner.invoke(cli.app, ["setup", "--skip-deps", "--skip-ollama-check"])
        assert result.exit_code == 0
        assert run.call_count == len(cli.DEFAULT_MODELS)
        for needle in ("nope", "Pull timed out", "kaboom"):
            assert needle in result.output

    def test_pull_models_ollama_missing_breaks(self) -> None:
        with patch.object(cli.subprocess, "run", side_effect=FileNotFoundError) as run:
            result = runner.invoke(cli.app, ["setup", "--skip-deps", "--skip-ollama-check"])
        assert "'ollama' command not found" in result.output
        assert run.call_count == 1


def _make_ext(root: Path) -> Path:
    ext = root / "vsix-extension"
    ext.mkdir(parents=True, exist_ok=True)
    (ext / "package.json").write_text("{}")
    return ext


class TestInstallExtension:
    def test_extension_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        with patch.object(Path, "exists", return_value=False):
            result = runner.invoke(cli.app, ["install-extension"])
        assert result.exit_code == 1
        assert "extension not found" in result.output

    def test_cli_install_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ext = _make_ext(tmp_path / "cwd")
        monkeypatch.chdir(ext.parent)
        with (
            patch.object(cli.shutil, "which", return_value="/usr/bin/code"),
            patch.object(cli.subprocess, "run", return_value=_proc(0)),
        ):
            result = runner.invoke(
                cli.app, ["install-extension", "--vscode-path", str(tmp_path / "ext")]
            )
        assert result.exit_code == 0
        assert "installed successfully" in result.output

    def test_cli_failure_falls_back_to_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ext = _make_ext(tmp_path / "cwd")
        monkeypatch.chdir(ext.parent)
        target_root = tmp_path / "ext"
        # Pre-existing target exercises the rmtree branch.
        (target_root / "penguincode.penguincode-vscode").mkdir(parents=True)
        with (
            patch.object(cli.shutil, "which", return_value="/usr/bin/code"),
            patch.object(cli.subprocess, "run", return_value=_proc(1, "cli broke")),
        ):
            result = runner.invoke(
                cli.app, ["install-extension", "--vscode-path", str(target_root)]
            )
        assert result.exit_code == 0
        assert "CLI install failed" in result.output
        assert (target_root / "penguincode.penguincode-vscode" / "package.json").exists()

    def test_cli_exception_falls_back_to_copy_default_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ext = _make_ext(tmp_path / "cwd")
        monkeypatch.chdir(ext.parent)
        with (
            patch.object(cli.shutil, "which", return_value="/usr/bin/code"),
            patch.object(cli.subprocess, "run", side_effect=OSError("exec")),
        ):
            result = runner.invoke(cli.app, ["install-extension"])
        assert result.exit_code == 0
        assert "CLI install error" in result.output
        assert (
            tmp_path / ".vscode" / "extensions" / "penguincode.penguincode-vscode" / "package.json"
        ).exists()

    def test_manual_copy_failure_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ext = _make_ext(tmp_path / "cwd")
        monkeypatch.chdir(ext.parent)
        with (
            patch.object(cli.shutil, "which", return_value=None),
            patch.object(cli.shutil, "copytree", side_effect=OSError("disk")),
        ):
            result = runner.invoke(
                cli.app, ["install-extension", "--vscode-path", str(tmp_path / "ext")]
            )
        assert result.exit_code == 1
        assert "Installation failed" in result.output

    def test_darwin_branch_default_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ext = _make_ext(tmp_path / "cwd")
        monkeypatch.chdir(ext.parent)
        with (
            patch.object(cli.os, "uname", return_value=SimpleNamespace(sysname="Darwin")),
            patch.object(cli.shutil, "which", return_value=None),
            patch.object(cli.shutil, "copytree"),
        ):
            result = runner.invoke(cli.app, ["install-extension"])
        assert result.exit_code == 0
        assert "Extension copied" in result.output


class TestMain:
    def test_main_invokes_app(self) -> None:
        with patch.object(cli, "app") as app:
            cli.main()
        app.assert_called_once_with()

    def test_module_guard(self) -> None:
        import runpy

        with patch.object(sys, "argv", ["penguincode", "--help"]):
            with pytest.raises(SystemExit) as exc:
                runpy.run_module("penguincode_cli.main", run_name="__main__")
        assert exc.value.code == 0
