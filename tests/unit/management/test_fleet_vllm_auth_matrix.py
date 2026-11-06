"""401/403 matrix over every vLLM / llama.cpp / Ollama fleet management route (AM19d).

Routes are generated from the blueprint's URL map at collection time, so a new endpoint under
one of the fleet prefixes is covered without editing this file -- and a route that lacks
``require_auth`` / ``require_scope(FLEET_ADMIN)`` fails its case instead of shipping open.
"""

from __future__ import annotations

import re

import pytest
from quart import Quart

from services.management.app.api.v1 import api_v1_bp

NEW_PREFIXES = (
    "/api/v1/fleet/vllm",
    "/api/v1/fleet/llamacpp",
    "/api/v1/fleet/ollama",
    "/api/v1/fleet/models",
    "/api/v1/fleet/gpu-inventory",
    "/api/v1/fleet/savings",
    "/api/v1/fleet/chargeback",
)


def _collect() -> list[tuple[str, str]]:
    """(concrete path, method) for every route under a new fleet prefix."""
    probe = Quart("auth-matrix-probe")
    probe.register_blueprint(api_v1_bp, url_prefix="/api/v1")
    return sorted(
        (re.sub(r"<[^>]+>", "1", rule.rule), method)
        for rule in probe.url_map.iter_rules()
        if rule.rule.startswith(NEW_PREFIXES)
        for method in rule.methods - {"HEAD", "OPTIONS"}
    )


NEW_ROUTES = _collect()


def test_matrix_examines_routes() -> None:
    """Zero routes examined is a failure, never a vacuous pass."""
    assert NEW_ROUTES, "zero routes examined = FAIL"
    assert ("/api/v1/fleet/savings", "GET") in NEW_ROUTES
    assert ("/api/v1/fleet/vllm/chargeback", "GET") in NEW_ROUTES


@pytest.mark.parametrize(("path", "method"), NEW_ROUTES)
async def test_new_route_401_without_jwt(client, path: str, method: str) -> None:
    """No Authorization header -> 401, never a pass-through."""
    resp = await client.open(path, method=method)
    assert resp.status_code == 401


@pytest.mark.parametrize(("path", "method"), NEW_ROUTES)
async def test_new_route_403_without_fleet_admin(
    client, app_mock_db, user_auth_headers, path: str, method: str
) -> None:
    """A valid JWT lacking ``fleet:admin`` -> 403."""
    resp = await client.open(path, method=method, headers=user_auth_headers, json={})
    assert resp.status_code == 403
