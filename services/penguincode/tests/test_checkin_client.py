"""Tests for the standalone `client/checkin_client.py` script (O5: no-timeout fix).

Loaded via `importlib.util.spec_from_file_location` rather than `import client.checkin_client`
-- the repo also has a top-level `client.py` module (a different, unrelated dead script)
sharing the `client` name with the `client/` package, which makes `import client...`
ambiguous/broken regardless of this fix; loading this one file directly by path sidesteps
that unrelated, pre-existing collision entirely.

Covers: a timeout tuple is always passed to `requests.post`, env-var overrides of the
default timeout, and that every failure mode (timeout, connection error, non-200,
malformed JSON) degrades to `None` rather than raising.

# regression: ops-audit O5 (checkin_client.py had no timeout at all)
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest
import requests

_MODULE_PATH = Path(__file__).resolve().parents[1] / "client" / "checkin_client.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "penguincode_checkin_client_under_test", _MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def checkin_client() -> ModuleType:
    return _load_module()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "PENGUINCODE_CHECKIN_CONNECT_TIMEOUT_SECONDS",
        "PENGUINCODE_CHECKIN_READ_TIMEOUT_SECONDS",
        "PENGUINCODE_CHECKIN_URL",
    ):
        monkeypatch.delenv(name, raising=False)


class TestTimeoutAlwaysApplied:
    def test_default_timeout_tuple(self, checkin_client: ModuleType) -> None:
        response = MagicMock(status_code=200)
        response.json.return_value = {"ok": True}
        with patch.object(requests, "post", return_value=response) as mocked_post:
            result = checkin_client.checkin("user123")

        assert result == {"ok": True}
        _, kwargs = mocked_post.call_args
        assert kwargs["timeout"] == (5.0, 10.0)

    def test_env_overrides_timeout_tuple(
        self, checkin_client: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_CHECKIN_CONNECT_TIMEOUT_SECONDS", "1")
        monkeypatch.setenv("PENGUINCODE_CHECKIN_READ_TIMEOUT_SECONDS", "2")
        response = MagicMock(status_code=200)
        response.json.return_value = {}
        with patch.object(requests, "post", return_value=response) as mocked_post:
            checkin_client.checkin("user123")

        _, kwargs = mocked_post.call_args
        assert kwargs["timeout"] == (1.0, 2.0)

    def test_malformed_timeout_env_falls_back_to_default(
        self, checkin_client: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_CHECKIN_CONNECT_TIMEOUT_SECONDS", "not-a-number")
        response = MagicMock(status_code=200)
        response.json.return_value = {}
        with patch.object(requests, "post", return_value=response) as mocked_post:
            checkin_client.checkin("user123")

        _, kwargs = mocked_post.call_args
        assert kwargs["timeout"][0] == 5.0

    def test_url_overridable_via_env(
        self, checkin_client: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_CHECKIN_URL", "http://example.internal/checkin")
        response = MagicMock(status_code=200)
        response.json.return_value = {}
        with patch.object(requests, "post", return_value=response) as mocked_post:
            checkin_client.checkin("user123")

        args, _ = mocked_post.call_args
        assert args[0] == "http://example.internal/checkin"


class TestFailureModesSwallowed:
    def test_timeout_returns_none_never_raises(self, checkin_client: ModuleType) -> None:
        with patch.object(requests, "post", side_effect=requests.exceptions.Timeout("slow")):
            assert checkin_client.checkin("user123") is None

    def test_connection_error_returns_none(self, checkin_client: ModuleType) -> None:
        with patch.object(
            requests, "post", side_effect=requests.exceptions.ConnectionError("refused")
        ):
            assert checkin_client.checkin("user123") is None

    def test_non_200_returns_none(self, checkin_client: ModuleType) -> None:
        response = MagicMock(status_code=500)
        with patch.object(requests, "post", return_value=response):
            assert checkin_client.checkin("user123") is None

    def test_malformed_json_returns_none(self, checkin_client: ModuleType) -> None:
        response = MagicMock(status_code=200)
        response.json.side_effect = ValueError("bad json")
        with patch.object(requests, "post", return_value=response):
            assert checkin_client.checkin("user123") is None


class TestMainGuard:
    def test_module_is_importable_standalone(self, checkin_client: ModuleType) -> None:
        """Importing the module (via its __main__ guard path) must not itself make a
        network call -- `if __name__ == "__main__"` only runs under direct execution,
        never under `spec.loader.exec_module`'s module-name ("...under_test").
        """
        assert checkin_client.__name__ != "__main__"
        assert hasattr(checkin_client, "checkin")
