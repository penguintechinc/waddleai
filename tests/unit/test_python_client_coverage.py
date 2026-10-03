"""Coverage for the remaining PenguinTechLicenseClient surface untouched by this fix.

These methods (validate, keepalive, get_all_features, is_valid_license_key,
the module-level convenience functions) predate the stale-while-error fix in
tests/unit/test_license_stale_cache.py and had no dedicated unit tests; this
file brings the whole module up to the repo's 90% coverage bar.
"""

import time as _time
from unittest.mock import MagicMock, patch

import pytest
import requests

import shared.licensing.python_client as pc
from shared.licensing.python_client import (
    FeatureNotAvailableError,
    LicenseValidationError,
    PenguinTechLicenseClient,
    check_feature,
    get_client,
    initialize_licensing,
    requires_feature,
    send_keepalive,
)


@pytest.fixture(autouse=True)
def _reset_global_client():
    """Reset the module-level global client singleton before every test.

    An autouse fixture, not a bare `setup_function`: `setup_function` is only
    invoked by pytest for bare module-level test functions, NOT for methods on
    test classes -- and every test below lives in a class. Using it here would
    silently skip the reset for all of them, which is exactly how this file
    was vulnerable to a leaked `_global_client` (e.g. from
    test_license_client_timeout.py's TestInitializeLicensing, which assigns a
    MagicMock to the global during a now-exited `patch()` context) persisting
    across test collection order.
    """
    pc._global_client = None
    yield
    pc._global_client = None


@pytest.fixture
def client() -> PenguinTechLicenseClient:
    """A plain license client with no pre-populated cache."""
    return PenguinTechLicenseClient(license_key="lic-test", product="waddleai")


def _ok(payload: dict) -> MagicMock:
    """A 200-ish response stub whose json() returns `payload`."""
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


class TestValidate:
    """PenguinTechLicenseClient.validate()."""

    def test_success_stores_server_id_and_features(self, client: PenguinTechLicenseClient) -> None:
        """A valid response stores the server_id and seeds the feature cache."""
        with patch.object(client.session, "post") as post:
            post.return_value = _ok(
                {
                    "valid": True,
                    "metadata": {"server_id": "srv-1"},
                    "features": [{"name": "f1", "entitled": True}],
                }
            )
            data = client.validate()
        assert data["valid"] is True
        assert client.server_id == "srv-1"
        assert client._feature_cache == {"f1": True}

    def test_invalid_response_raises(self, client: PenguinTechLicenseClient) -> None:
        """A response with valid=False raises LicenseValidationError."""
        with patch.object(client.session, "post") as post:
            post.return_value = _ok({"valid": False, "message": "expired"})
            with pytest.raises(LicenseValidationError, match="expired"):
                client.validate()

    def test_request_exception_raises_license_validation_error(
        self, client: PenguinTechLicenseClient
    ) -> None:
        """A transport-level failure is wrapped as LicenseValidationError."""
        with patch.object(client.session, "post", side_effect=requests.ConnectionError("down")):
            with pytest.raises(LicenseValidationError):
                client.validate()


class TestKeepalive:
    """PenguinTechLicenseClient.keepalive()."""

    def test_keepalive_with_existing_server_id(self, client: PenguinTechLicenseClient) -> None:
        """With server_id already set, keepalive posts directly with no prior validate()."""
        client.server_id = "srv-1"
        with patch.object(client.session, "post") as post:
            post.return_value = _ok({"status": "ok"})
            assert client.keepalive({"requests": 5}) == {"status": "ok"}

    def test_keepalive_validates_first_when_no_server_id(
        self, client: PenguinTechLicenseClient
    ) -> None:
        """With no server_id yet, keepalive validates first to obtain one."""
        with patch.object(
            client, "validate", return_value={"valid": True, "server_id": "srv-2"}
        ) as validate:
            with patch.object(client.session, "post") as post:
                post.return_value = _ok({"status": "ok"})
                client.keepalive()
        validate.assert_called_once()

    def test_keepalive_raises_when_prior_validation_invalid(
        self, client: PenguinTechLicenseClient
    ) -> None:
        """If the implicit validate() comes back invalid, keepalive raises."""
        with patch.object(client, "validate", return_value={"valid": False}):
            with pytest.raises(LicenseValidationError):
                client.keepalive()

    def test_keepalive_request_exception_raises(self, client: PenguinTechLicenseClient) -> None:
        """A transport-level failure is wrapped as LicenseValidationError."""
        client.server_id = "srv-1"
        with patch.object(client.session, "post", side_effect=requests.Timeout("slow")):
            with pytest.raises(LicenseValidationError):
                client.keepalive()


class TestGetAllFeatures:
    """PenguinTechLicenseClient.get_all_features()."""

    def test_returns_cached_copy_when_valid(self, client: PenguinTechLicenseClient) -> None:
        """A fresh cache is returned as a copy, with no network call."""
        client._feature_cache = {"f1": True}
        client._cache_timestamp = _time.time()
        assert client.get_all_features() == {"f1": True}

    def test_refreshes_via_validate_when_stale(self, client: PenguinTechLicenseClient) -> None:
        """With no cache yet, get_all_features triggers a validate() to refresh it."""
        with patch.object(client, "validate") as validate:
            client.get_all_features()
        validate.assert_called_once()

    def test_logs_and_returns_empty_when_refresh_fails(
        self, client: PenguinTechLicenseClient
    ) -> None:
        """A failed refresh logs and returns whatever (empty) cache exists."""
        with patch.object(client, "validate", side_effect=LicenseValidationError("down")):
            assert client.get_all_features() == {}


class TestIsValidLicenseKey:
    """PenguinTechLicenseClient.is_valid_license_key() format validation."""

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("PENG-AAAA-BBBB-CCCC-DDDD-EEFF", True),
            ("", False),
            ("TOO-SHORT", False),
            ("X" * 29, False),
            ("PENG-AAAABBBBCCCCDDDDEEFF-----", False),
        ],
    )
    def test_format_validation(self, key: str, expected: bool) -> None:
        """Only a PENG-prefixed, 29-char, 5-dash key is valid."""
        assert PenguinTechLicenseClient.is_valid_license_key(key) == expected


class TestFromEnv:
    """PenguinTechLicenseClient.from_env()."""

    def test_missing_env_vars_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With LICENSE_KEY/PRODUCT_NAME unset, from_env returns None, not a broken client."""
        monkeypatch.delenv("LICENSE_KEY", raising=False)
        monkeypatch.delenv("PRODUCT_NAME", raising=False)
        assert PenguinTechLicenseClient.from_env() is None

    def test_present_env_vars_builds_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With both env vars present, from_env builds a client from them."""
        monkeypatch.setenv("LICENSE_KEY", "lic-env")
        monkeypatch.setenv("PRODUCT_NAME", "waddleai")
        c = PenguinTechLicenseClient.from_env()
        assert c is not None
        assert c.license_key == "lic-env"


class TestModuleLevelConvenience:
    """The module-level get_client/check_feature/send_keepalive convenience functions."""

    def test_get_client_builds_from_env_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_client() memoizes the global client across calls."""
        monkeypatch.setenv("LICENSE_KEY", "lic-env")
        monkeypatch.setenv("PRODUCT_NAME", "waddleai")
        first = get_client()
        second = get_client()
        assert first is second

    def test_check_feature_module_fn_no_client_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no client configured, the module-level check_feature() is False."""
        monkeypatch.delenv("LICENSE_KEY", raising=False)
        monkeypatch.delenv("PRODUCT_NAME", raising=False)
        assert check_feature("anything") is False

    def test_check_feature_module_fn_delegates_to_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With a client configured, check_feature() delegates to it."""
        monkeypatch.setenv("LICENSE_KEY", "lic-env")
        monkeypatch.setenv("PRODUCT_NAME", "waddleai")
        with patch.object(PenguinTechLicenseClient, "check_feature", return_value=True):
            assert check_feature("f1") is True

    def test_send_keepalive_no_client_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no client configured, send_keepalive() is False."""
        monkeypatch.delenv("LICENSE_KEY", raising=False)
        monkeypatch.delenv("PRODUCT_NAME", raising=False)
        assert send_keepalive() is False

    def test_send_keepalive_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A successful keepalive() call makes send_keepalive() return True."""
        monkeypatch.setenv("LICENSE_KEY", "lic-env")
        monkeypatch.setenv("PRODUCT_NAME", "waddleai")
        with patch.object(PenguinTechLicenseClient, "keepalive", return_value={"status": "ok"}):
            assert send_keepalive({"n": 1}) is True

    def test_send_keepalive_failure_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failing keepalive() call makes send_keepalive() return False, not raise."""
        monkeypatch.setenv("LICENSE_KEY", "lic-env")
        monkeypatch.setenv("PRODUCT_NAME", "waddleai")
        with patch.object(
            PenguinTechLicenseClient, "keepalive", side_effect=LicenseValidationError("down")
        ):
            assert send_keepalive() is False


class TestRequiresFeatureDecorator:
    """The @requires_feature license-gate decorator."""

    def test_allows_call_when_entitled(self) -> None:
        """An entitled feature lets the wrapped function run normally."""
        fake_client = MagicMock()
        fake_client.check_feature.return_value = True

        @requires_feature("premium", client=fake_client)
        def protected() -> str:
            return "ok"

        assert protected() == "ok"

    def test_raises_when_not_entitled(self) -> None:
        """A non-entitled feature raises FeatureNotAvailableError instead of calling through."""
        fake_client = MagicMock()
        fake_client.check_feature.return_value = False

        @requires_feature("premium", client=fake_client)
        def protected() -> str:
            return "ok"

        with pytest.raises(FeatureNotAvailableError):
            protected()

    def test_raises_when_no_client_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no client (explicit or global), the decorator denies rather than erroring."""
        monkeypatch.delenv("LICENSE_KEY", raising=False)
        monkeypatch.delenv("PRODUCT_NAME", raising=False)

        @requires_feature("premium")
        def protected() -> str:
            return "ok"

        with pytest.raises(FeatureNotAvailableError):
            protected()


class TestInitializeLicensingValidationFlow:
    """initialize_licensing()'s happy path."""

    def test_success_logs_and_returns_validation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A successful validate() call returns its response verbatim."""
        monkeypatch.setenv("LICENSE_KEY", "lic-env")
        monkeypatch.setenv("PRODUCT_NAME", "waddleai")
        validation = {
            "customer": "acme",
            "tier": "enterprise",
            "features": [{"name": "f1", "entitled": True}],
        }
        with patch.object(PenguinTechLicenseClient, "validate", return_value=validation):
            result = initialize_licensing()
        assert result == validation


def test_record_license_check_swallows_telemetry_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken metrics backend must never break a license check."""
    with patch("shared.observability.metrics.get_meter", side_effect=RuntimeError("no meter")):
        pc._record_license_check("live")  # must not raise
