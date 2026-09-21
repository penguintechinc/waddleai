"""A 500 must never hand the client a traceback or source code.

audit-2026-09-14 follow-up. Quart's DEBUG mode replaces the registered 500
handler with an interactive traceback page that embeds the exception message,
the file paths and the actual SOURCE LINES of the failing module -- roughly
24KB of internals per error. ProductionConfig pins DEBUG=False, so deployed
services return the sanitized JSON body from register_error_handlers() instead.

These tests pin that difference in place. The production assertion is the one
that matters; the DEBUG assertion is here so that if someone ever makes the
debug page the default, this file says loudly what that would mean rather than
the behaviour changing silently.
"""

import pytest

from services.management.app.config import ProductionConfig, TestingConfig

SENTINEL = "sentinel-internal-detail-8f3a1c"


async def _error_body(config_class, monkeypatch, tmp_path):
    """Build an app on the given config, raise inside a route, return the body."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/leak.db")
    monkeypatch.setenv("CACHE_HOST", "")

    import services.management.app as app_module

    monkeypatch.setattr(app_module, "init_extensions", lambda app: None)
    app = app_module.create_app(config_class)

    @app.route("/__boom_probe")
    async def _boom():
        raise RuntimeError(SENTINEL)

    response = await app.test_client().get("/__boom_probe")
    body = (await response.get_data()).decode("utf-8", "replace")
    return response, body


class TestProductionErrorResponses:
    """The deployed configuration leaks nothing."""

    async def test_production_500_body_is_sanitized_json(self, monkeypatch, tmp_path):
        """A 500 in production returns the generic JSON body, nothing more."""
        response, body = await _error_body(ProductionConfig, monkeypatch, tmp_path)

        assert response.status_code == 500
        assert SENTINEL not in body, "the exception message reached the client"
        assert "Traceback" not in body
        assert "RuntimeError" not in body
        assert ".py" not in body, "a source path reached the client"
        assert "An unexpected error occurred" in body

    async def test_production_500_body_is_small(self, monkeypatch, tmp_path):
        """Size is the cheapest proxy for "this is not a traceback page".

        The debug page is ~24KB; the sanitized body is well under 200 bytes.
        A regression here would be obvious long before anyone read the content.
        """
        _, body = await _error_body(ProductionConfig, monkeypatch, tmp_path)
        assert len(body) < 512, f"500 body grew to {len(body)} bytes — is DEBUG on?"

    def test_production_config_pins_debug_off(self):
        """DEBUG=False is hardcoded, not read from FLASK_DEBUG.

        This is what makes the guarantee above hold regardless of what the
        environment sets.
        """
        assert ProductionConfig.DEBUG is False


class TestDebugConfigsAreNotDeployable:
    """Documents, in an executable way, why DEBUG must stay off in deployments."""

    @pytest.mark.parametrize("config_class", [TestingConfig])
    async def test_debug_config_does_leak_internals(self, config_class, monkeypatch, tmp_path):
        """DEBUG=True serves the exception message and source — never deploy it.

        Asserting the leak rather than ignoring it: this is the behaviour that
        made a contract-test 500 dump credential_encryption.py's source into a
        response body.
        """
        _, body = await _error_body(config_class, monkeypatch, tmp_path)
        assert config_class.DEBUG is True
        assert SENTINEL in body, (
            "DEBUG no longer leaks the exception message — if the debug page was "
            "disabled deliberately, delete this test and say so"
        )
