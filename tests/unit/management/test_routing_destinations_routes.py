"""Unit tests for /api/v1/routing/destinations and /destination-credentials.

Covers the two-layer Enterprise gate (flag off -> 404, unentitled -> 403),
org resolution (S1: cross-org requires PROVIDER_ADMIN), IDOR-safe 404s for
rows outside the resolved org, BYOK ownership (S2 write: provider match +
platform-or-same-org), the <=5-enabled-per-model cap (S7), and per-type
credential masking (S4: bearer vs bedrock, secret material never returned).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from services.management.app.api.v1 import routing_destinations as rd
from tests.unit.management.conftest import make_dal_row, make_select_result

DESTINATIONS_PATH = "/api/v1/routing/destinations"
CREDENTIALS_PATH = "/api/v1/routing/destination-credentials"


# ---------------------------------------------------------------------------
# Pure helpers -- no app/db needed
# ---------------------------------------------------------------------------


def test_mask_material_bearer():
    """Bearer material is masked via `_mask_key`, keeping a short prefix/suffix."""
    assert rd._mask_material("openai", "enc:sk-abcdefghij") == rd._mask_material(
        "openai", "enc:sk-abcdefghij"
    )
    masked = rd._mask_material("openai", "sk-abcdefghij")
    assert masked.startswith("sk-a") and masked.endswith("ghij") and "****" in masked


def test_mask_material_bedrock_masks_only_access_key_id():
    """Bedrock JSON material: only aws_access_key_id is shown (masked); the secret never leaks."""
    material = json.dumps(
        {"aws_access_key_id": "AKIAEXAMPLE1234", "aws_secret_access_key": "supersecret"}
    )
    masked = rd._mask_material("bedrock", material)
    assert "supersecret" not in masked  # secret never leaks
    assert "aws_access_key_id" in masked
    assert "AKIA" in masked and "****" in masked  # only the access-key id is shown, masked


def test_mask_material_empty_returns_empty_string():
    """No stored material (None or empty) masks to an empty string, never an exception."""
    assert rd._mask_material("openai", None) == ""
    assert rd._mask_material("openai", "") == ""


def test_mask_material_bedrock_malformed_json_never_raises():
    """Malformed bedrock JSON masks to a fixed placeholder instead of raising."""
    assert rd._mask_material("bedrock", "not-json") == "****"


def test_resolve_org_rejects_cross_org_without_provider_admin():
    """A non-PROVIDER_ADMIN caller requesting another org is refused (403, no org)."""
    g_user = {"organization_id": 7, "scopes": []}
    org, err = rd._resolve_org(g_user, requested_org=99, has_provider_admin=False)
    assert org is None and err == 403


def test_resolve_org_allows_cross_org_with_provider_admin():
    """A PROVIDER_ADMIN caller may resolve to a different org."""
    g_user = {"organization_id": 7}
    org, err = rd._resolve_org(g_user, requested_org=99, has_provider_admin=True)
    assert org == 99 and err is None


def test_resolve_org_defaults_to_token_org():
    """With no requested org, the token's own org is used."""
    org, err = rd._resolve_org({"organization_id": 7}, requested_org=None, has_provider_admin=False)
    assert org == 7 and err is None


def test_resolve_org_same_org_requested_is_not_a_mismatch():
    """Requesting your own org explicitly is never treated as cross-org."""
    org, err = rd._resolve_org({"organization_id": 7}, requested_org=7, has_provider_admin=False)
    assert org == 7 and err is None


def test_validate_ownership_matrix():
    """S2: same-provider + (platform-owned or same-org) is OK; provider/org mismatch is not."""
    # same provider + platform credential (owner None) -> ok
    assert rd._validate_ownership({"provider_id": 3, "owner_org_id": None}, 3, 7) is None
    # same provider + same-org credential -> ok
    assert rd._validate_ownership({"provider_id": 3, "owner_org_id": 7}, 3, 7) is None
    # provider mismatch -> error
    assert rd._validate_ownership({"provider_id": 4, "owner_org_id": 7}, 3, 7) is not None
    # other-org credential -> error
    assert rd._validate_ownership({"provider_id": 3, "owner_org_id": 99}, 3, 7) is not None


def test_validate_material_bedrock_requires_both_keys():
    """Bedrock material must be JSON with both aws_access_key_id and aws_secret_access_key."""
    assert rd._validate_material("bedrock", json.dumps({"aws_access_key_id": "AKIA"})) is not None
    both_keys = json.dumps({"aws_access_key_id": "AKIA", "aws_secret_access_key": "s"})
    assert rd._validate_material("bedrock", both_keys) is None


def test_validate_material_bearer_requires_non_empty():
    """Bearer-type material must be a non-empty string."""
    assert rd._validate_material("openai", "") is not None
    assert rd._validate_material("openai", "sk-abc") is None


def test_validate_material_unsupported_provider_type_rejected():
    """A provider type outside bedrock/_BEARER_TYPES is rejected for BYOK credentials."""
    assert rd._validate_material("ollama", "anything") is not None


# ---------------------------------------------------------------------------
# Two-layer gate helper -- needs an app context for jsonify(), not db
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_flag_off_is_404(monkeypatch, flask_app):
    """S12: the provider_failover flag off makes the surface 404 (fail-safe hide)."""
    monkeypatch.setenv("WADDLEAI_FLAG_PROVIDER_FAILOVER", "0")
    async with flask_app.app_context():
        _body, status = await rd._gate(7)
    assert status == 404


@pytest.mark.asyncio
async def test_gate_unentitled_is_403(monkeypatch, flask_app):
    """S12: flag on but no Enterprise entitlement refuses with 403 (fail-closed)."""
    monkeypatch.setenv("WADDLEAI_FLAG_PROVIDER_FAILOVER", "1")
    monkeypatch.setattr(
        rd, "_get_license_client", lambda: type("L", (), {"check_feature": lambda self, f: False})()
    )
    async with flask_app.app_context():
        _body, status = await rd._gate(7)
    assert status == 403


@pytest.mark.asyncio
async def test_gate_open_returns_none(monkeypatch, flask_app):
    """Flag on and entitled: the gate returns None (caller proceeds)."""
    monkeypatch.setenv("WADDLEAI_FLAG_PROVIDER_FAILOVER", "1")
    monkeypatch.setattr(
        rd, "_get_license_client", lambda: type("L", (), {"check_feature": lambda self, f: True})()
    )
    async with flask_app.app_context():
        assert await rd._gate(7) is None


# ---------------------------------------------------------------------------
# Route-level tests (Quart test client)
# ---------------------------------------------------------------------------


def _enable_flag(monkeypatch) -> None:
    """Turn `waddleai.provider_failover` on for the duration of one test."""
    monkeypatch.setenv("WADDLEAI_FLAG_PROVIDER_FAILOVER", "1")


def _entitled(monkeypatch, entitled: bool = True) -> None:
    """Patch the license-entitlement check for one test."""
    mock_client = MagicMock()
    mock_client.check_feature.return_value = entitled
    monkeypatch.setattr(
        "services.management.app.api.v1.routing_destinations._get_license_client",
        lambda: mock_client,
    )


def _gate_open(monkeypatch, entitled: bool = True) -> None:
    """Flag on + entitled -- the surface is fully usable."""
    _enable_flag(monkeypatch)
    _entitled(monkeypatch, entitled)


def _make_provider_row(
    *, provider_id: int = 1, provider_type: str = "openai", enabled: bool = True
) -> MagicMock:
    """Build a spec'd fake `ai_providers` row."""
    return make_dal_row(id=provider_id, provider_type=provider_type, enabled=enabled)


def _make_credential_row(
    *,
    cred_id: int = 1,
    provider_id: int = 1,
    owner_org_id: int | None = None,
    label: str = "my-key",
    api_key: str = "enc:sk-abcdefghij",
    enabled: bool = True,
) -> MagicMock:
    """Build a spec'd fake `provider_credentials` row."""
    return make_dal_row(
        id=cred_id,
        provider_id=provider_id,
        owner_org_id=owner_org_id,
        label=label,
        api_key=api_key,
        enabled=enabled,
    )


class TestGateRoutes:
    """S12: the two-layer gate applies to every route in this module."""

    async def test_list_destinations_flag_off_is_404(self, client, auth_headers, monkeypatch):
        """GET destinations with the flag off is 404."""
        monkeypatch.setenv("WADDLEAI_FLAG_PROVIDER_FAILOVER", "0")
        resp = await client.get(DESTINATIONS_PATH, headers=auth_headers)
        assert resp.status_code == 404

    async def test_list_destinations_unentitled_is_403(self, client, auth_headers, monkeypatch):
        """GET destinations with the flag on but unentitled is 403."""
        _gate_open(monkeypatch, entitled=False)
        resp = await client.get(DESTINATIONS_PATH, headers=auth_headers)
        assert resp.status_code == 403

    async def test_list_credentials_flag_off_is_404(self, client, auth_headers, monkeypatch):
        """GET destination-credentials with the flag off is 404."""
        monkeypatch.setenv("WADDLEAI_FLAG_PROVIDER_FAILOVER", "0")
        resp = await client.get(CREDENTIALS_PATH, headers=auth_headers)
        assert resp.status_code == 404

    async def test_create_destination_flag_off_is_404(self, client, auth_headers, monkeypatch):
        """POST destinations with the flag off is 404."""
        monkeypatch.setenv("WADDLEAI_FLAG_PROVIDER_FAILOVER", "0")
        resp = await client.post(
            DESTINATIONS_PATH, headers=auth_headers, json={"model": "x", "provider_id": 1}
        )
        assert resp.status_code == 404


class TestIDORSafety:
    """S1/IDOR: a row addressed by id outside the resolved org is 404, never 403."""

    async def test_patch_destination_cross_org_id_is_404(
        self, client, app_mock_db, auth_headers, monkeypatch
    ):
        """Admin (org=1) PATCHing a destination that belongs to another org -> 404, not 403."""
        _gate_open(monkeypatch)
        app_mock_db.return_value.select.return_value.first.return_value = None  # not in this org
        resp = await client.patch(
            f"{DESTINATIONS_PATH}/999", headers=auth_headers, json={"priority": 1}
        )
        assert resp.status_code == 404

    async def test_delete_destination_cross_org_id_is_404(
        self, client, app_mock_db, auth_headers, monkeypatch
    ):
        """Admin DELETEing a destination outside their org -> 404 (no existence leak)."""
        _gate_open(monkeypatch)
        app_mock_db.return_value.select.return_value.first.return_value = None
        resp = await client.delete(f"{DESTINATIONS_PATH}/999", headers=auth_headers)
        assert resp.status_code == 404

    async def test_delete_credential_cross_org_id_is_404(
        self, client, app_mock_db, auth_headers, monkeypatch
    ):
        """Admin DELETEing a credential outside their org -> 404 (no existence leak)."""
        _gate_open(monkeypatch)
        app_mock_db.return_value.select.return_value.first.return_value = None
        resp = await client.delete(f"{CREDENTIALS_PATH}/999", headers=auth_headers)
        assert resp.status_code == 404


class TestOrgResolution:
    """S1: a non-admin caller cannot override organization_id to another org."""

    async def test_non_admin_organization_id_mismatch_is_403(
        self, client, app_mock_db, rm_auth_headers, monkeypatch
    ):
        """resource_manager (no PROVIDER_ADMIN) requesting org=99 while token org=1 -> 403."""
        _gate_open(monkeypatch)
        resp = await client.get(f"{DESTINATIONS_PATH}?organization_id=99", headers=rm_auth_headers)
        assert resp.status_code == 403

    async def test_admin_organization_id_cross_org_allowed(
        self, client, app_mock_db, auth_headers, monkeypatch
    ):
        """Admin (holds PROVIDER_ADMIN) may query another org's destinations."""
        _gate_open(monkeypatch)
        app_mock_db.return_value.select.return_value = make_select_result([])
        resp = await client.get(f"{DESTINATIONS_PATH}?organization_id=99", headers=auth_headers)
        assert resp.status_code == 200


class TestCredentialOwnershipWrite:
    """S2 write: a destination may only reference a same-provider platform/own-org credential."""

    async def test_create_destination_cross_org_credential_is_422(
        self, client, app_mock_db, auth_headers, monkeypatch
    ):
        """Creating a destination referencing another org's BYOK credential is 422."""
        _gate_open(monkeypatch)
        provider = _make_provider_row(provider_id=1, enabled=True)
        other_org_cred = _make_credential_row(cred_id=5, provider_id=1, owner_org_id=99)
        app_mock_db.return_value.select.return_value.first.side_effect = [provider, other_org_cred]

        resp = await client.post(
            DESTINATIONS_PATH,
            headers=auth_headers,
            json={"model": "gpt-4o", "provider_id": 1, "credential_id": 5},
        )
        assert resp.status_code == 422

    async def test_create_destination_provider_mismatch_credential_is_422(
        self, client, app_mock_db, auth_headers, monkeypatch
    ):
        """Creating a destination whose credential.provider_id differs is 422."""
        _gate_open(monkeypatch)
        provider = _make_provider_row(provider_id=1, enabled=True)
        mismatched_cred = _make_credential_row(cred_id=5, provider_id=2, owner_org_id=None)
        app_mock_db.return_value.select.return_value.first.side_effect = [provider, mismatched_cred]

        resp = await client.post(
            DESTINATIONS_PATH,
            headers=auth_headers,
            json={"model": "gpt-4o", "provider_id": 1, "credential_id": 5},
        )
        assert resp.status_code == 422

    async def test_update_destination_cross_org_credential_is_422(
        self, client, app_mock_db, auth_headers, monkeypatch
    ):
        """PATCH re-pointing credential_id to another org's credential is also 422 (S2)."""
        _gate_open(monkeypatch)
        existing = make_dal_row(
            id=1,
            organization_id=1,
            model="gpt-4o",
            priority=0,
            provider_id=1,
            credential_id=None,
            enabled=True,
        )
        other_org_cred = _make_credential_row(cred_id=5, provider_id=1, owner_org_id=99)
        app_mock_db.return_value.select.return_value.first.side_effect = [existing, other_org_cred]

        resp = await client.patch(
            f"{DESTINATIONS_PATH}/1", headers=auth_headers, json={"credential_id": 5}
        )
        assert resp.status_code == 422


class TestEnabledCap:
    """S7: at most MAX_DESTINATIONS_PER_MODEL (5) enabled destinations per (org, model)."""

    async def test_sixth_enabled_destination_is_422(
        self, client, app_mock_db, auth_headers, monkeypatch
    ):
        """A 6th enabled destination for the same (org, model) is refused with 422."""
        _gate_open(monkeypatch)
        provider = _make_provider_row(provider_id=1, enabled=True)
        app_mock_db.return_value.select.return_value.first.side_effect = [provider, None]
        app_mock_db.return_value.count.return_value = rd.MAX_DESTINATIONS_PER_MODEL

        resp = await client.post(
            DESTINATIONS_PATH,
            headers=auth_headers,
            json={"model": "gpt-4o", "provider_id": 1, "priority": 5, "enabled": True},
        )
        assert resp.status_code == 422

    async def test_fifth_enabled_destination_is_allowed(
        self, client, app_mock_db, auth_headers, monkeypatch
    ):
        """The cap is <=5 -- a 5th enabled destination (count currently 4) is allowed."""
        _gate_open(monkeypatch)
        provider = _make_provider_row(provider_id=1, enabled=True)
        created = make_dal_row(
            id=10,
            organization_id=1,
            model="gpt-4o",
            priority=4,
            provider_id=1,
            credential_id=None,
            provider_model_id=None,
            region=None,
            timeout_seconds=None,
            enabled=True,
        )
        app_mock_db.return_value.select.return_value.first.side_effect = [provider, None, created]
        app_mock_db.return_value.count.return_value = rd.MAX_DESTINATIONS_PER_MODEL - 1
        app_mock_db.model_destinations.insert.return_value = 10

        resp = await client.post(
            DESTINATIONS_PATH,
            headers=auth_headers,
            json={"model": "gpt-4o", "provider_id": 1, "priority": 4, "enabled": True},
        )
        assert resp.status_code == 201


class TestCredentialMaskingResponses:
    """S4: plaintext credential material never appears in a response body."""

    async def test_list_credentials_never_contains_material(
        self, client, app_mock_db, auth_headers, monkeypatch
    ):
        """The credentials list response never leaks stored key material."""
        _gate_open(monkeypatch)
        stored = "enc:sk-supersecretvalue"  # noqa: S105 -- test fixture, not a real credential
        cred = _make_credential_row(cred_id=1, provider_id=1, owner_org_id=1, api_key=stored)
        provider = _make_provider_row(provider_id=1, provider_type="openai")
        result = make_select_result([cred])
        result.first.return_value = provider  # the nested per-row provider-type lookup
        app_mock_db.return_value.select.return_value = result

        resp = await client.get(CREDENTIALS_PATH, headers=auth_headers)
        assert resp.status_code == 200
        body = await resp.get_data(as_text=True)
        assert "supersecretvalue" not in body

    async def test_create_credential_response_never_contains_material(
        self, client, app_mock_db, auth_headers, monkeypatch
    ):
        """The create-credential response never echoes the plaintext material back."""
        _gate_open(monkeypatch)
        provider = _make_provider_row(provider_id=1, provider_type="openai", enabled=True)
        created = _make_credential_row(
            cred_id=9, provider_id=1, owner_org_id=1, api_key="enc:sk-brandnewsecretvalue"
        )
        app_mock_db.return_value.select.return_value.first.side_effect = [provider, created]
        app_mock_db.provider_credentials.insert.return_value = 9

        resp = await client.post(
            CREDENTIALS_PATH,
            headers=auth_headers,
            json={"provider_id": 1, "label": "prod-key", "material": "sk-brandnewsecretvalue"},
        )
        assert resp.status_code == 201
        body = await resp.get_data(as_text=True)
        assert "brandnewsecretvalue" not in body

    async def test_create_bedrock_credential_response_never_contains_secret(
        self, client, app_mock_db, auth_headers, monkeypatch
    ):
        """S4: a bedrock secret_access_key is never returned, only the masked access-key id."""
        _gate_open(monkeypatch)
        provider = _make_provider_row(provider_id=2, provider_type="bedrock", enabled=True)
        material = json.dumps(
            {"aws_access_key_id": "AKIASOMEKEYID123", "aws_secret_access_key": "topsecretvalue"}
        )
        created = _make_credential_row(
            cred_id=11, provider_id=2, owner_org_id=1, api_key=f"enc:{material}"
        )
        app_mock_db.return_value.select.return_value.first.side_effect = [provider, created]
        app_mock_db.provider_credentials.insert.return_value = 11

        resp = await client.post(
            CREDENTIALS_PATH,
            headers=auth_headers,
            json={"provider_id": 2, "label": "bedrock-key", "material": material},
        )
        assert resp.status_code == 201
        body = await resp.get_data(as_text=True)
        assert "topsecretvalue" not in body
        assert "AKIA" in body  # masked access-key id IS shown
