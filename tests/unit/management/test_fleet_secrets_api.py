"""C2c: per-deployment bearer tokens, cluster secret list and rotation."""

from __future__ import annotations

import logging
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from services.management.app.services import fleet_secrets
from shared.security.credential_encryption import decrypt_credential
from tests.unit.management.test_fleet_vllm_deployments import DEPS, _body
from tests.unit.management.vllm_harness import (
    seed_backend,
    seed_deployment,
    seed_gpus,
    seed_rate,
)

SENTINEL = "SENTINEL-TOKEN-VALUE-do-not-leak"


@pytest.fixture
def ready(vllm_db, both_flags, fake_backend, fake_k8s, license_tier, monkeypatch):
    """Backend, GPUs, a rate row and 'present' weights."""
    backend = seed_backend(vllm_db)
    seed_gpus(vllm_db, backend, count=4)
    seed_rate(vllm_db)
    monkeypatch.setattr(
        "services.management.app.api.v1.fleet_vllm.model_store.ensure_weights",
        AsyncMock(return_value="/models/x"),
    )
    return backend


async def _deployment(client, headers, backend):
    resp = await client.post(DEPS, headers=headers, json=_body(backend))
    assert resp.status_code == 201
    return (await resp.get_json())["data"]


async def test_rotate_token_patches_only_slug_key_without_serve_put(
    client, ready, auth_headers, fake_k8s, vllm_db, fake_backend
):
    """Rotation patches only data.<slug>; no backend call; link value + fingerprint change."""
    dep = await _deployment(client, auth_headers, ready)
    old_token = fake_k8s.deployment_key(dep["slug"])
    fake_k8s.patches.clear()
    fake_backend.calls.clear()
    resp = await client.post(f"{DEPS}/{dep['public_id']}/rotate-token", headers=auth_headers)
    assert resp.status_code == 200
    data = (await resp.get_json())["data"]
    assert [set(patch) for _name, patch in fake_k8s.patches] == [{dep["slug"]}]
    new_token = fake_k8s.deployment_key(dep["slug"])
    assert new_token != old_token and fake_backend.calls == []
    assert (
        data["api_key_fingerprint"]
        == fleet_secrets.fingerprint(new_token)
        != dep["api_key_fingerprint"]
    )
    link = vllm_db(vllm_db.connection_links.id > 0).select().first()
    assert decrypt_credential(link.api_key) == new_token
    assert new_token not in str(data)


async def test_rotate_token_secret_patch_failure_keeps_old_link(
    client, ready, auth_headers, fake_k8s, vllm_db
):
    """A failed Secret write is 502 and the old link value stays (never half-rotated)."""
    dep = await _deployment(client, auth_headers, ready)
    before = vllm_db(vllm_db.connection_links.id > 0).select().first().api_key
    fake_k8s.fail_next_patch = True
    resp = await client.post(f"{DEPS}/{dep['public_id']}/rotate-token", headers=auth_headers)
    assert resp.status_code == 502
    assert (await resp.get_json())["reason"] == "secret write failed"
    assert vllm_db(vllm_db.connection_links.id > 0).select().first().api_key == before


async def test_rotate_token_logs_audit_event_without_value(
    client, ready, auth_headers, fake_k8s, caplog
):
    """The structured event carries fingerprints, never the token."""
    dep = await _deployment(client, auth_headers, ready)
    with caplog.at_level(logging.INFO):
        await client.post(f"{DEPS}/{dep['public_id']}/rotate-token", headers=auth_headers)
    token = fake_k8s.deployment_key(dep["slug"])
    messages = [r.getMessage() for r in caplog.records if "token_rotated" in r.getMessage()]
    assert len(messages) == 1 and fleet_secrets.fingerprint(token) in messages[0]
    assert token not in caplog.text


async def test_rotate_token_foreign_org_and_unknown_are_both_404(
    client, ready, vllm_db, auth_headers
):
    """Another org's deployment and an unknown id are both 404 (no existence oracle)."""
    other = seed_backend(vllm_db, org_id=2, name="o2")
    foreign = seed_deployment(vllm_db, other, org_id=2, name="f")
    assert (
        await client.post(f"{DEPS}/{foreign.public_id}/rotate-token", headers=auth_headers)
    ).status_code == 404
    import uuid

    assert (
        await client.post(f"{DEPS}/{uuid.uuid4()}/rotate-token", headers=auth_headers)
    ).status_code == 404


async def test_delete_removes_slug_key_from_secret(client, ready, auth_headers, fake_k8s):
    """DELETE merge-patches data.<slug> to null."""
    dep = await _deployment(client, auth_headers, ready)
    assert fake_k8s.deployment_key(dep["slug"])
    resp = await client.delete(f"{DEPS}/{dep['public_id']}", headers=auth_headers)
    assert resp.status_code == 200
    assert fake_k8s.patches[-1][1] == {dep["slug"]: None}
    assert fake_k8s.deployment_key(dep["slug"]) is None


SECRETS = "/api/v1/fleet/secrets"


@pytest.fixture
def operator_headers(mint_token, monkeypatch):
    """The platform operator: an admin token whose org is the configured system org."""
    monkeypatch.setenv("WADDLEAI_SYSTEM_ORG_ID", "99")
    return {"Authorization": f"Bearer {mint_token(role='admin', org_id=99)}"}


@pytest.fixture
def cluster(vllm_db, both_flags, fake_k8s, monkeypatch):
    """Ray secrets present and no external-source env vars."""
    for var in (
        "WADDLEAI_RAY_AUTH_TOKEN", "WADDLEAI_RAY_AUTH_TOKEN_EXISTING_SECRET",
        "WADDLEAI_RAY_TLS_CERT_PATH", "WADDLEAI_RAY_TLS_KEY_PATH", "WADDLEAI_RAY_TLS_CA_PATH",
        "WADDLEAI_RAY_TLS_EXISTING_SECRET",
    ):  # fmt: skip
        monkeypatch.delenv(var, raising=False)
    fake_k8s.secrets[fleet_secrets.RAY_AUTH_SECRET] = {"token": SENTINEL}
    fake_k8s.secrets[fleet_secrets.RAY_TLS_SECRET] = {"tls.crt": "cert-body"}
    return fake_k8s


async def test_secrets_list_returns_source_fingerprint_rotated_at_never_value(
    client, cluster, operator_headers, vllm_db
):
    """Names, source, fingerprint and rotated_at (from the audit trail); no value key."""
    vllm_db.audit_log.insert(
        method="POST", path="fleet.secret.rotated/ray-auth-token", status_code=200,
        created_at=datetime(2026, 11, 1, 12, 0, 0),
    )  # fmt: skip
    vllm_db.commit()
    resp = await client.get(SECRETS, headers=operator_headers)
    assert resp.status_code == 200
    items = {i["name"]: i for i in (await resp.get_json())["data"]}
    assert set(items) == {"ray-auth-token", "ray-internal-tls"}
    assert items["ray-auth-token"]["fingerprint"] == fleet_secrets.fingerprint(SENTINEL)
    assert items["ray-auth-token"]["rotated_at"].startswith("2026-11-01T12:00")
    assert items["ray-internal-tls"]["rotated_at"] is None
    assert all(set(i) == {"name", "source", "fingerprint", "rotated_at"} for i in items.values())
    assert all(i["source"] == "generated" for i in items.values())


async def test_secrets_list_requires_operator_not_org_admin(
    client, cluster, user_auth_headers, auth_headers, operator_headers
):
    """An org admin and a plain user are 403; no JWT is 401 (cluster keys are not org-scoped)."""
    assert (await client.get(SECRETS, headers=auth_headers)).status_code == 403
    assert (await client.get(SECRETS, headers=user_auth_headers)).status_code == 403
    assert (await client.get(SECRETS)).status_code == 401
    assert (await client.get(SECRETS, headers=operator_headers)).status_code == 200


async def test_secrets_unconfigured_system_org_fails_closed(
    client, cluster, mint_token, monkeypatch
):
    """With no system org configured nobody (even admin scope) can touch cluster secrets."""
    monkeypatch.delenv("WADDLEAI_SYSTEM_ORG_ID", raising=False)
    headers = {"Authorization": f"Bearer {mint_token(role='admin', org_id=99)}"}
    assert (await client.get(SECRETS, headers=headers)).status_code == 403
    assert (
        await client.post(f"{SECRETS}/ray-auth-token/rotate", headers=headers)
    ).status_code == 403
    assert cluster.patches == [] and cluster.restart == {}


async def test_org_admin_cannot_rotate_cluster_secret(
    client, cluster, auth_headers, operator_headers
):
    """An org admin's rotate is 403 and writes nothing."""
    resp = await client.post(f"{SECRETS}/ray-auth-token/rotate", headers=auth_headers)
    assert resp.status_code == 403
    assert cluster.patches == [] and cluster.restart == {} and cluster.deleted_secrets == []


async def test_rotation_writes_audit_row_with_fingerprints_not_values(
    client, cluster, operator_headers, vllm_db
):
    """Every rotation leaves an audit row: actor, secret name, fingerprint old->new."""
    resp = await client.post(f"{SECRETS}/ray-auth-token/rotate", headers=operator_headers)
    assert resp.status_code == 200
    new = cluster.secrets[fleet_secrets.RAY_AUTH_SECRET]["token"]
    row = vllm_db(vllm_db.audit_log.path == "fleet.secret.rotated/ray-auth-token").select().first()
    assert row.organization_id == 99 and row.user_id == 1 and row.status_code == 200
    assert row.resource_id == (
        f"ray-auth-token:{fleet_secrets.fingerprint(SENTINEL)}->{fleet_secrets.fingerprint(new)}"
    )
    assert SENTINEL not in row.resource_id and new not in row.resource_id


async def test_rapid_repeat_rotation_is_429_until_cooldown(
    client, cluster, operator_headers, vllm_db
):
    """A second rotation of the same secret within 5 minutes is 429 (other secrets unaffected)."""
    url = f"{SECRETS}/ray-auth-token/rotate"
    assert (await client.post(url, headers=operator_headers)).status_code == 200
    again = await client.post(url, headers=operator_headers)
    assert again.status_code == 429 and again.headers.get("Retry-After")
    assert cluster.restart == {"RayCluster/waddleai-vllm-ray": 1}
    tls = await client.post(f"{SECRETS}/ray-internal-tls/rotate", headers=operator_headers)
    assert tls.status_code == 200
    from datetime import datetime, timedelta

    vllm_db(vllm_db.audit_log.path == "fleet.secret.rotated/ray-auth-token").update(
        created_at=datetime.utcnow() - timedelta(minutes=6)
    )
    vllm_db.commit()
    assert (await client.post(url, headers=operator_headers)).status_code == 200


async def test_failed_rotation_does_not_start_cooldown(client, cluster, operator_headers):
    """A failed Secret write releases the claim so an immediate retry is allowed."""
    cluster.fail_next_patch = True
    url = f"{SECRETS}/ray-auth-token/rotate"
    assert (await client.post(url, headers=operator_headers)).status_code == 502
    assert (await client.post(url, headers=operator_headers)).status_code == 200


async def test_secrets_flag_off_is_404_for_the_operator(
    client, cluster, operator_headers, monkeypatch
):
    """Even the operator gets 404 while the vLLM flags are off."""
    monkeypatch.delenv("WADDLEAI_FLAG_FLEET_VLLM_RAY")
    assert (await client.get(SECRETS, headers=operator_headers)).status_code == 404
    rotate = await client.post(f"{SECRETS}/ray-auth-token/rotate", headers=operator_headers)
    assert rotate.status_code == 404


async def test_secrets_external_env_reports_external_and_rotate_is_409(
    client, cluster, operator_headers, monkeypatch
):
    """An env-supplied key is external; rotation is 409 and nothing is written."""
    monkeypatch.setenv("WADDLEAI_RAY_AUTH_TOKEN", "from-vault")
    listed = {
        i["name"]: i
        for i in (await (await client.get(SECRETS, headers=operator_headers)).get_json())["data"]
    }
    assert listed["ray-auth-token"]["source"] == "external"
    resp = await client.post(f"{SECRETS}/ray-auth-token/rotate", headers=operator_headers)
    assert resp.status_code == 409 and (await resp.get_json())["reason"] == "managed externally"
    assert cluster.patches == [] and cluster.restart == {}


async def test_secrets_rotate_generated_bumps_restart_generation(client, cluster, operator_headers):
    """Rotating the Ray token writes a 48-char alnum token, bumps the RayCluster generation."""
    resp = await client.post(f"{SECRETS}/ray-auth-token/rotate", headers=operator_headers)
    assert resp.status_code == 200
    new = cluster.secrets[fleet_secrets.RAY_AUTH_SECRET]["token"]
    assert len(new) == 48 and new.isalnum() and new != SENTINEL
    assert cluster.restart == {"RayCluster/waddleai-vllm-ray": 1}


async def test_secrets_rotate_tls_deletes_secret_for_reissue(client, cluster, operator_headers):
    """TLS rotation deletes the Secret so cert-manager reissues it, and rolls the pods."""
    resp = await client.post(f"{SECRETS}/ray-internal-tls/rotate", headers=operator_headers)
    assert resp.status_code == 200
    assert fleet_secrets.RAY_TLS_SECRET in cluster.deleted_secrets
    assert cluster.restart["RayCluster/waddleai-vllm-ray"] == 1


async def test_secrets_rotate_unknown_name_404_and_write_failure_502(
    client, cluster, operator_headers
):
    """Unknown name is 404; a failed write is 502 and nothing is rolled."""
    assert (
        await client.post(f"{SECRETS}/nope/rotate", headers=operator_headers)
    ).status_code == 404
    cluster.fail_next_patch = True
    resp = await client.post(f"{SECRETS}/ray-auth-token/rotate", headers=operator_headers)
    assert resp.status_code == 502 and cluster.restart == {}


async def test_secrets_rotate_auth_matrix(client, cluster, user_auth_headers, operator_headers):
    """401 without JWT, 403 without fleet:admin."""
    assert (await client.post(f"{SECRETS}/ray-auth-token/rotate")).status_code == 401
    assert (
        await client.post(f"{SECRETS}/ray-auth-token/rotate", headers=user_auth_headers)
    ).status_code == 403


async def test_value_never_in_any_response_or_log(client, cluster, operator_headers, caplog):
    """The sentinel token is absent from list, rotate responses and the log."""
    with caplog.at_level(logging.DEBUG):
        bodies = [
            await (await client.get(SECRETS, headers=operator_headers)).get_data(),
            await (
                await client.post(f"{SECRETS}/ray-auth-token/rotate", headers=operator_headers)
            ).get_data(),
            await (await client.get(SECRETS, headers=operator_headers)).get_data(),
        ]
    new = cluster.secrets[fleet_secrets.RAY_AUTH_SECRET]["token"]
    for body in bodies:
        assert SENTINEL.encode() not in body and new.encode() not in body
    assert SENTINEL not in caplog.text and new not in caplog.text
