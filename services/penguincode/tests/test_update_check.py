"""Tests for `client/update_check.py` -- the O8 silent, non-blocking startup version check.

Covers: server-ahead (notice), server-behind/equal (no notice), server unreachable
(returns `None`, never raises), the `penguincode.disable-update-check` kill-switch, and
the interval gate (`_should_run_check`) that keeps a fresh CLI process from re-checking
every single startup.

# regression: ops-audit O8 (CLI resilience -- startup update check)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import pytest

from penguincode_cli.client.update_check import (
    _parse_version,
    _should_run_check,
    check_for_update,
    maybe_notify_update,
)
from penguincode_cli.config.settings import ClientConfig, ServerConfig


def _server_config() -> ServerConfig:
    return ServerConfig(host="pc-server.internal", port=50051)


class TestParseVersion:
    def test_simple_dotted_version(self) -> None:
        assert _parse_version("1.2.3") == (1, 2, 3)

    def test_non_numeric_suffix_truncates(self) -> None:
        assert _parse_version("1.2.3-beta") == (1, 2)

    def test_empty_string(self) -> None:
        assert _parse_version("") == ()

    def test_comparison_behind(self) -> None:
        assert _parse_version("1.0.0") < _parse_version("1.1.0")

    def test_comparison_equal(self) -> None:
        assert _parse_version("1.0.0") == _parse_version("1.0.0")


class TestCheckForUpdate:
    async def test_server_ahead_reports_update_available(self) -> None:
        channel = MagicMock()
        channel.close = AsyncMock()
        stub_check = AsyncMock(return_value=MagicMock(version="2.0.0"))

        with (
            patch("grpc.aio.insecure_channel", return_value=channel),
            patch("penguincode_cli.client.update_check.HealthServiceStub") as mocked_stub,
        ):
            mocked_stub.return_value.Check = stub_check
            result = await check_for_update(_server_config(), current_version="1.0.0")

        assert result is not None
        assert result.update_available is True
        assert result.server_version == "2.0.0"
        assert result.current_version == "1.0.0"
        channel.close.assert_awaited_once()

    async def test_server_behind_reports_no_update(self) -> None:
        channel = MagicMock()
        channel.close = AsyncMock()
        stub_check = AsyncMock(return_value=MagicMock(version="1.0.0"))

        with (
            patch("grpc.aio.insecure_channel", return_value=channel),
            patch("penguincode_cli.client.update_check.HealthServiceStub") as mocked_stub,
        ):
            mocked_stub.return_value.Check = stub_check
            result = await check_for_update(_server_config(), current_version="2.0.0")

        assert result is not None
        assert result.update_available is False

    async def test_server_equal_reports_no_update(self) -> None:
        channel = MagicMock()
        channel.close = AsyncMock()
        stub_check = AsyncMock(return_value=MagicMock(version="1.5.0"))

        with (
            patch("grpc.aio.insecure_channel", return_value=channel),
            patch("penguincode_cli.client.update_check.HealthServiceStub") as mocked_stub,
        ):
            mocked_stub.return_value.Check = stub_check
            result = await check_for_update(_server_config(), current_version="1.5.0")

        assert result is not None
        assert result.update_available is False

    async def test_server_unreachable_returns_none_never_raises(self) -> None:
        channel = MagicMock()
        channel.close = AsyncMock()
        stub_check = AsyncMock(
            side_effect=grpc.aio.AioRpcError(
                grpc.StatusCode.UNAVAILABLE, grpc.aio.Metadata(), grpc.aio.Metadata()
            )
        )

        with (
            patch("grpc.aio.insecure_channel", return_value=channel),
            patch("penguincode_cli.client.update_check.HealthServiceStub") as mocked_stub,
        ):
            mocked_stub.return_value.Check = stub_check
            result = await check_for_update(_server_config(), current_version="1.0.0")

        assert result is None
        channel.close.assert_awaited_once()  # channel still cleaned up on failure

    async def test_tls_enabled_uses_secure_channel(self) -> None:
        server_config = ServerConfig(host="pc-server.internal", port=50051, tls_enabled=True)
        channel = MagicMock()
        channel.close = AsyncMock()
        stub_check = AsyncMock(return_value=MagicMock(version="1.0.0"))

        with (
            patch("grpc.ssl_channel_credentials", return_value=MagicMock()),
            patch("grpc.aio.secure_channel", return_value=channel) as mocked_secure,
            patch("penguincode_cli.client.update_check.HealthServiceStub") as mocked_stub,
        ):
            mocked_stub.return_value.Check = stub_check
            await check_for_update(server_config, current_version="1.0.0")

        mocked_secure.assert_called_once()


class TestIntervalGate:
    def test_missing_state_file_always_runs(self, tmp_path: Path) -> None:
        assert _should_run_check(tmp_path / "missing.json", interval_hours=24.0, now=1000.0)

    def test_recent_state_file_skips(self, tmp_path: Path) -> None:
        state = tmp_path / "state.json"
        state.write_text("{}", encoding="utf-8")
        now = state.stat().st_mtime + 10  # 10s later, well within a 24h interval
        assert not _should_run_check(state, interval_hours=24.0, now=now)

    def test_expired_state_file_runs_again(self, tmp_path: Path) -> None:
        state = tmp_path / "state.json"
        state.write_text("{}", encoding="utf-8")
        now = state.stat().st_mtime + (25 * 3600)  # past the 24h interval
        assert _should_run_check(state, interval_hours=24.0, now=now)


class TestMaybeNotifyUpdate:
    async def test_kill_switch_skips_entirely(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_DISABLE_UPDATE_CHECK", "true")
        with patch("penguincode_cli.client.update_check.check_for_update") as mocked_check:
            notice = await maybe_notify_update(
                _server_config(),
                ClientConfig(),
                state_path=str(tmp_path / "state.json"),
            )
        assert notice is None
        mocked_check.assert_not_called()

    async def test_returns_notice_when_update_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PENGUINCODE_FLAG_DISABLE_UPDATE_CHECK", raising=False)
        channel = MagicMock()
        channel.close = AsyncMock()
        stub_check = AsyncMock(return_value=MagicMock(version="9.9.9"))

        with (
            patch("grpc.aio.insecure_channel", return_value=channel),
            patch("penguincode_cli.client.update_check.HealthServiceStub") as mocked_stub,
        ):
            mocked_stub.return_value.Check = stub_check
            notice = await maybe_notify_update(
                _server_config(),
                ClientConfig(),
                state_path=str(tmp_path / "state.json"),
                clock=1000.0,
            )

        assert notice is not None
        assert "9.9.9" in notice
        assert (tmp_path / "state.json").exists()  # check was recorded

    async def test_returns_none_when_already_checked_recently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PENGUINCODE_FLAG_DISABLE_UPDATE_CHECK", raising=False)
        state_path = tmp_path / "state.json"
        state_path.write_text("{}", encoding="utf-8")

        with patch("penguincode_cli.client.update_check.check_for_update") as mocked_check:
            notice = await maybe_notify_update(
                _server_config(),
                ClientConfig(),
                state_path=str(state_path),
                clock=state_path.stat().st_mtime + 1,
            )

        assert notice is None
        mocked_check.assert_not_called()
