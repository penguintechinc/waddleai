"""regression: cross-org IDOR on llama.cpp deployment routes (rotate-token and legacy id routes)."""

from __future__ import annotations

import pytest

from services.management.app.services import fleet_secrets
from tests.unit.management.test_local_pricing_api import LLAMA, _llama, priced  # noqa: F401

ROTATE = "/api/v1/fleet/llamacpp/deployments/{}/rotate-token"
LEGACY = "/api/v1/llamacpp/deployments"


@pytest.fixture
def org2_headers(mint_token) -> dict[str, str]:
    """Admin bearer for a SECOND organization (id=2)."""
    token = mint_token(role="admin", user_id=30, org_id=2)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def _create(client, headers):
    return await (await client.post(LLAMA, headers=headers, json=_llama())).get_json()


async def test_cross_org_rotate_is_404_and_touches_nothing(
    client,
    priced,  # noqa: F811
    auth_headers,
    org2_headers,
    fake_k8s,  # noqa: F811
):
    """Org 2 rotating org 1's deployment: same 404 as missing; no Secret/link/restart effect."""
    created = await _create(client, auth_headers)
    secret_before = dict(fake_k8s.secrets[fleet_secrets.DEPLOYMENT_KEYS_SECRET])
    link_name = f"llamacpp-{created['public_id']}"
    link_before = priced(priced.connection_links.name == link_name).select().first().api_key
    missing = await client.post(
        ROTATE.format("00000000-0000-0000-0000-000000000099"), headers=org2_headers
    )
    resp = await client.post(ROTATE.format(created["public_id"]), headers=org2_headers)
    assert resp.status_code == 404
    assert await resp.get_json() == await missing.get_json()
    assert fake_k8s.secrets[fleet_secrets.DEPLOYMENT_KEYS_SECRET] == secret_before
    assert priced(priced.connection_links.name == link_name).select().first().api_key == link_before
    assert fake_k8s.restart == {}


async def test_owner_org_can_still_rotate(client, priced, auth_headers, fake_k8s):  # noqa: F811
    """Positive control: the owning org's rotate still succeeds."""
    created = await _create(client, auth_headers)
    resp = await client.post(ROTATE.format(created["public_id"]), headers=auth_headers)
    assert resp.status_code == 200 and list(fake_k8s.restart.values()) == [1]


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("get", ""),
        ("patch", ""),
        ("delete", ""),
        ("post", "/deploy"),
        ("post", "/remove"),
        ("get", "/health"),
        ("get", "/export/k8s"),
    ],
)
async def test_cross_org_legacy_routes_404(
    client,
    priced,  # noqa: F811
    auth_headers,
    org2_headers,
    method,
    suffix,  # noqa: F811
):
    """Every id-keyed llama.cpp route hides another org's deployment behind 404."""
    created = await _create(client, auth_headers)
    url = f"{LEGACY}/{created['deployment_id']}{suffix}"
    resp = await getattr(client, method)(url, headers=org2_headers, json={"n_ctx": 1})
    assert resp.status_code == 404
    assert priced(priced.llamacpp_deployments.id == created["deployment_id"]).count() == 1
