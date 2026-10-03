"""MANAGEMENT_MAX_BODY_BYTES caps request bodies; oversized requests 413 in the standard shape.

O6 (ops audit): the management Quart app had no MAX_CONTENT_LENGTH, so an
unbounded request body could be read fully into memory before any handler
ever saw it. Quart/werkzeug enforce the configured cap and raise
RequestEntityTooLarge; this just pins the env-driven default and the JSON
error shape of our 413 handler.
"""

import pytest
from quart import request

from services.management.app.config import TestingConfig

_SENTINEL_ROUTE = "/__body_size_probe"


async def _build_app(monkeypatch, tmp_path, max_body_bytes: int | None = None):
    """Build a management app with mocked extensions and a probe echo route."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/body.db")
    monkeypatch.setenv("CACHE_HOST", "")
    if max_body_bytes is not None:
        monkeypatch.setenv("MANAGEMENT_MAX_BODY_BYTES", str(max_body_bytes))
    else:
        monkeypatch.delenv("MANAGEMENT_MAX_BODY_BYTES", raising=False)

    import importlib

    from services.management.app import config as config_module

    importlib.reload(config_module)

    import services.management.app as app_module

    monkeypatch.setattr(app_module, "init_extensions", lambda app: None)
    app = app_module.create_app(config_module.TestingConfig)

    @app.route(_SENTINEL_ROUTE, methods=["POST"])
    async def _probe():
        data = await request.get_data()
        return {"received": len(data)}, 200

    return app


def test_config_max_content_length_defaults_to_2mib() -> None:
    """With no env override, the cap defaults to 2 MiB."""
    assert TestingConfig.MAX_CONTENT_LENGTH == 2 * 1024 * 1024


async def test_oversized_body_returns_413_in_standard_error_shape(monkeypatch, tmp_path) -> None:
    """A body larger than MANAGEMENT_MAX_BODY_BYTES 413s with {"error", "message"}."""
    app = await _build_app(monkeypatch, tmp_path, max_body_bytes=1024)

    oversized = b"x" * 4096
    response = await app.test_client().post(
        _SENTINEL_ROUTE,
        data=oversized,
        headers={"Content-Length": str(len(oversized))},
    )

    assert response.status_code == 413
    body = await response.get_json()
    assert body["error"] == "Payload Too Large"
    assert "1024" in body["message"]


async def test_body_within_limit_is_accepted(monkeypatch, tmp_path) -> None:
    """A body at or under the configured cap is processed normally."""
    app = await _build_app(monkeypatch, tmp_path, max_body_bytes=4096)

    payload = b"y" * 100
    response = await app.test_client().post(
        _SENTINEL_ROUTE,
        data=payload,
        headers={"Content-Length": str(len(payload))},
    )

    assert response.status_code == 200
    body = await response.get_json()
    assert body["received"] == 100


@pytest.fixture(autouse=True)
def _reset_config_module_after_test():
    """Reload config once more after each test so later modules see a clean env-derived Config."""
    yield
    import importlib

    from services.management.app import config as config_module

    importlib.reload(config_module)
