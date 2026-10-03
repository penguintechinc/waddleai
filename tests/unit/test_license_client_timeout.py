"""Regression tests: every license-server call carries an explicit timeout.

`PenguinTechLicenseClient.__init__` used to do `self.session.timeout = timeout`.
`requests.Session` accepts the attribute but `Session.request()` never reads it
(verified against requests 2.34.2), so the setting bought nothing and every call
to license.penguintech.io was unbounded — a hung or blackholed license server
could block the caller indefinitely on a path that is supposed to degrade
gracefully. These tests pin the per-request `timeout=` kwarg, the only form
requests honours.
"""

from unittest.mock import MagicMock, patch

import pytest

import shared.licensing.python_client as pc
from shared.licensing.python_client import (
    LicenseValidationError,
    PenguinTechLicenseClient,
    initialize_licensing,
)

_TIMEOUT = 7


@pytest.fixture(autouse=True)
def _reset_global_client():
    """initialize_licensing() sets the module-level _global_client singleton.

    TestInitializeLicensing patches PenguinTechLicenseClient's constructor,
    but that patch unwinds at the `with` block's exit -- the *assignment* of
    its MagicMock return value to `pc._global_client` is not undone by
    unpatching the class, so without this reset the leaked MagicMock
    survives into whichever test runs next in the session and permanently
    satisfies `get_client()`'s `if _global_client is None` check.
    """
    yield
    pc._global_client = None


@pytest.fixture
def client() -> PenguinTechLicenseClient:
    """A license client with a distinctive timeout, so a default can't masquerade as a pass."""
    return PenguinTechLicenseClient(license_key="lic-test", product="waddleai", timeout=_TIMEOUT)


def _ok_response(payload: dict) -> MagicMock:
    """A 200-ish response stub whose json() returns `payload`."""
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def test_validate_passes_timeout(client: PenguinTechLicenseClient) -> None:
    """validate() sends timeout= on its POST."""
    with patch.object(client.session, "post") as post:
        post.return_value = _ok_response({"valid": True, "features": []})
        client.validate()

    assert post.call_args.kwargs["timeout"] == _TIMEOUT


def test_check_feature_passes_timeout(client: PenguinTechLicenseClient) -> None:
    """The feature-entitlement POST sends timeout=."""
    with patch.object(client.session, "post") as post:
        post.return_value = _ok_response({"features": [{"name": "waddleai", "entitled": True}]})
        assert client.check_feature("waddleai", use_cache=False) is True

    assert post.call_args.kwargs["timeout"] == _TIMEOUT


def test_keepalive_passes_timeout(client: PenguinTechLicenseClient) -> None:
    """The keepalive POST sends timeout=.

    server_id is pre-set so keepalive() does not first route through validate()
    -- that would leave the assertion reading validate()'s call, not this one.
    """
    client.server_id = "srv-1"
    with patch.object(client.session, "post") as post:
        post.return_value = _ok_response({"status": "ok"})
        client.keepalive()

    assert post.call_count == 1
    assert post.call_args.args[0].endswith("/api/v2/keepalive")
    assert post.call_args.kwargs["timeout"] == _TIMEOUT


def test_session_timeout_attribute_is_not_relied_on(client: PenguinTechLicenseClient) -> None:
    """The Session must not carry a `timeout` attribute standing in for the real thing.

    Guards the exact regression: setting it looks like configuring a timeout,
    reads as configured in review, and does nothing at runtime.
    """
    assert not hasattr(client.session, "timeout")


class TestInitializeLicensing:
    """`initialize_licensing` has no call sites in the repo and had no tests.

    That combination is why a silent regression here would ship unnoticed: while
    fixing type annotations, resolving the env-var fallback into new local names
    left the client being constructed from the *unresolved* parameters, so an
    env-only configuration would have built a client with license_key=None. The
    credential-plumbing test below is the one that catches that.
    """

    def _validating_client(self) -> MagicMock:
        """A stub client whose validate() returns a minimal valid response."""
        client = MagicMock()
        client.validate.return_value = {"customer": "acme", "tier": "enterprise", "features": []}
        return client

    def test_uses_explicit_arguments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit license_key/product win over the environment."""
        monkeypatch.setenv("LICENSE_KEY", "env-key")
        monkeypatch.setenv("PRODUCT_NAME", "env-product")
        with patch("shared.licensing.python_client.PenguinTechLicenseClient") as ctor:
            ctor.return_value = self._validating_client()
            initialize_licensing(license_key="arg-key", product="arg-product")

        assert ctor.call_args.args == ("arg-key", "arg-product")

    def test_falls_back_to_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no arguments, the env vars are resolved AND actually passed through.

        Regression guard: asserting only that no exception was raised would pass
        even if the client were constructed with (None, None).
        """
        monkeypatch.setenv("LICENSE_KEY", "env-key")
        monkeypatch.setenv("PRODUCT_NAME", "env-product")
        with patch("shared.licensing.python_client.PenguinTechLicenseClient") as ctor:
            ctor.return_value = self._validating_client()
            initialize_licensing()

        assert ctor.call_args.args == ("env-key", "env-product")

    def test_raises_when_credentials_are_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Neither argument nor env var present -> LicenseValidationError, no client built."""
        monkeypatch.delenv("LICENSE_KEY", raising=False)
        monkeypatch.delenv("PRODUCT_NAME", raising=False)
        with patch("shared.licensing.python_client.PenguinTechLicenseClient") as ctor:
            with pytest.raises(LicenseValidationError):
                initialize_licensing()

        ctor.assert_not_called()
