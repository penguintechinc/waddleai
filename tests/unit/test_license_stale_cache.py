"""Unit tests for PenguinTechLicenseClient.check_feature's stale-while-error fallback.

A license-server outage after a prior successful fetch must serve the
last-known entitlement (up to LICENSE_MAX_STALE_SECONDS old) rather than
hard-denying an already-entitled tenant. Hard-denial is reserved for a
feature that was never successfully fetched, or whose cached entry has aged
past the max-stale window.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

import shared.utils.feature_flags as ff
from shared.licensing.python_client import PenguinTechLicenseClient

_FEATURE = "hybrid_targets"


def setup_function() -> None:
    """Reset the feature-flags module state (kill switch resolution) between tests."""
    ff.reset_for_testing()


@pytest.fixture
def client() -> PenguinTechLicenseClient:
    """A license client with a short cache TTL so staleness is easy to trigger."""
    c = PenguinTechLicenseClient(license_key="lic-test", product="waddleai")
    c._cache_ttl = 0  # every check_feature call re-fetches instead of serving a fresh hit
    return c


def _ok_response(entitled: bool) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {"features": [{"name": _FEATURE, "entitled": entitled}]}
    resp.raise_for_status.return_value = None
    return resp


def test_outage_after_success_serves_stale_entitlement(
    client: PenguinTechLicenseClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A RequestException after a successful fetch serves the last-known True, not False."""
    monkeypatch.delenv("WADDLEAI_FLAG_DISABLE_LICENSE_STALE_CACHE", raising=False)
    with patch.object(client.session, "post") as post:
        post.return_value = _ok_response(True)
        assert client.check_feature(_FEATURE, use_cache=False) is True

        post.side_effect = requests.ConnectionError("license server unreachable")
        assert client.check_feature(_FEATURE, use_cache=False) is True


def test_never_fetched_hard_denies_on_outage(
    client: PenguinTechLicenseClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A feature with no prior successful fetch hard-denies on a RequestException."""
    monkeypatch.delenv("WADDLEAI_FLAG_DISABLE_LICENSE_STALE_CACHE", raising=False)
    with patch.object(client.session, "post") as post:
        post.side_effect = requests.ConnectionError("license server unreachable")
        assert client.check_feature(_FEATURE, use_cache=False) is False


def test_stale_entry_past_max_stale_window_hard_denies(
    client: PenguinTechLicenseClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached entitlement older than LICENSE_MAX_STALE_SECONDS is denied, not served."""
    monkeypatch.delenv("WADDLEAI_FLAG_DISABLE_LICENSE_STALE_CACHE", raising=False)
    monkeypatch.setenv("LICENSE_MAX_STALE_SECONDS", "10")
    with patch.object(client.session, "post") as post:
        post.return_value = _ok_response(True)
        assert client.check_feature(_FEATURE, use_cache=False) is True

        assert client._cache_timestamp is not None
        client._cache_timestamp -= 20  # simulate the cached entry aging past the window

        post.side_effect = requests.ConnectionError("license server unreachable")
        assert client.check_feature(_FEATURE, use_cache=False) is False


def test_fresh_cache_hit_never_calls_the_server(
    client: PenguinTechLicenseClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """use_cache=True with a TTL-fresh entry serves it without a network call."""
    monkeypatch.delenv("WADDLEAI_FLAG_DISABLE_LICENSE_STALE_CACHE", raising=False)
    client._cache_ttl = 300
    with patch.object(client.session, "post") as post:
        post.return_value = _ok_response(True)
        assert client.check_feature(_FEATURE, use_cache=True) is True
        assert client.check_feature(_FEATURE, use_cache=True) is True
    post.assert_called_once()


def test_kill_switch_reverts_to_hard_deny_on_outage(
    client: PenguinTechLicenseClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """waddleai.disable-license-stale-cache ON skips stale serving, hard-denies immediately."""
    monkeypatch.setenv("WADDLEAI_FLAG_DISABLE_LICENSE_STALE_CACHE", "1")
    with patch.object(client.session, "post") as post:
        post.return_value = _ok_response(True)
        assert client.check_feature(_FEATURE, use_cache=False) is True

        post.side_effect = requests.ConnectionError("license server unreachable")
        assert client.check_feature(_FEATURE, use_cache=False) is False


def test_no_features_in_response_returns_false(
    client: PenguinTechLicenseClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty features list in a successful response is a definite False, not an error."""
    monkeypatch.delenv("WADDLEAI_FLAG_DISABLE_LICENSE_STALE_CACHE", raising=False)
    with patch.object(client.session, "post") as post:
        resp = MagicMock()
        resp.json.return_value = {"features": []}
        resp.raise_for_status.return_value = None
        post.return_value = resp
        assert client.check_feature(_FEATURE, use_cache=False) is False


def test_max_stale_seconds_invalid_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unparseable LICENSE_MAX_STALE_SECONDS logs a warning and uses the 7-day default."""
    from shared.licensing.python_client import _max_stale_seconds

    monkeypatch.setenv("LICENSE_MAX_STALE_SECONDS", "not-a-number")
    assert _max_stale_seconds() == 7 * 24 * 60 * 60
