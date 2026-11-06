"""Plan C review fixes: rotation atomicity, secret leaks, pricing sink, no existence oracle."""

from __future__ import annotations

import asyncio
import logging
import uuid
from unittest.mock import AsyncMock

import pytest

from services.management.app.services import fleet_secrets, model_store
from shared.security.credential_encryption import decrypt_credential
from tests.unit.management.test_fleet_vllm_deployments import DEPS, _body
from tests.unit.management.test_local_pricing_api import LLAMA, _llama, priced  # noqa: F401
from tests.unit.management.vllm_harness import (
    FakeK8s,
    seed_backend,
    seed_deployment,
    seed_gpus,
    seed_rate,
)

SECRETS = "/api/v1/fleet/secrets"
FV = "services.management.app.api.v1.fleet_vllm"


@pytest.fixture
def ready(vllm_db, both_flags, fake_backend, fake_k8s, license_tier, monkeypatch):
    """Backend, GPUs, a rate row and 'present' weights."""
    backend = seed_backend(vllm_db)
    seed_gpus(vllm_db, backend, count=4)
    seed_rate(vllm_db)
    monkeypatch.setattr(f"{FV}.model_store.ensure_weights", AsyncMock(return_value="/models/x"))
    return backend


async def _dep(client, headers, backend, **over):
    resp = await client.post(DEPS, headers=headers, json=_body(backend, **over))
    assert resp.status_code == 201
    return (await resp.get_json())["data"]


def _link(db):
    return db(db.connection_links.id > 0).select().first()


# --- 3: vLLM rotate-token is never half-rotated ---------------------------------------------


async def test_rotate_link_update_failure_restores_old_secret_and_is_a_distinct_502(
    client, ready, auth_headers, fake_k8s, vllm_db, monkeypatch, caplog
):
    """Secret written, link update fails -> old Secret restored, distinct 502, slug logged."""
    dep = await _dep(client, auth_headers, ready)
    old = fake_k8s.deployment_key(dep["slug"])
    old_link = _link(vllm_db).api_key

    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(f"{FV}.update_connection_link_api_key", boom)
    with caplog.at_level(logging.ERROR):
        resp = await client.post(f"{DEPS}/{dep['public_id']}/rotate-token", headers=auth_headers)
    body = await resp.get_json()
    assert resp.status_code == 502
    assert body["reason"] == "link update failed; previous token restored"
    assert fake_k8s.deployment_key(dep["slug"]) == old
    assert _link(vllm_db).api_key == old_link
    assert dep["slug"] in caplog.text and old not in caplog.text


async def test_rotate_without_connection_link_fails_loudly_before_any_write(
    client, ready, auth_headers, fake_k8s, vllm_db
):
    """No connection_link_id: refuse (409) and leave the Secret untouched."""
    dep = await _dep(client, auth_headers, ready)
    old = fake_k8s.deployment_key(dep["slug"])
    vllm_db(vllm_db.vllm_deployments.slug == dep["slug"]).update(connection_link_id=None)
    vllm_db.commit()
    fake_k8s.patches.clear()
    resp = await client.post(f"{DEPS}/{dep['public_id']}/rotate-token", headers=auth_headers)
    assert resp.status_code == 409
    assert fake_k8s.patches == [] and fake_k8s.deployment_key(dep["slug"]) == old


async def test_rotate_success_writes_an_audit_row_without_values(
    client, ready, auth_headers, fake_k8s, vllm_db
):
    """A successful rotation leaves an audit_log row with both fingerprints, never a token."""
    dep = await _dep(client, auth_headers, ready)
    old = fake_k8s.deployment_key(dep["slug"])
    resp = await client.post(f"{DEPS}/{dep['public_id']}/rotate-token", headers=auth_headers)
    assert resp.status_code == 200
    new = fake_k8s.deployment_key(dep["slug"])
    row = (
        vllm_db(vllm_db.audit_log.path == f"fleet.deployment.token_rotated/{dep['slug']}")
        .select()
        .first()
    )
    assert row is not None and row.status_code == 200
    assert row.resource_id == f"{fleet_secrets.fingerprint(old)}->{fleet_secrets.fingerprint(new)}"
    assert old not in row.resource_id and new not in row.resource_id


# --- 4: llama.cpp rotate: link follows the Secret, restart failure is distinct ----------------


async def test_llamacpp_rotate_restart_failure_keeps_link_in_step_and_says_restart_pending(
    client,
    priced,  # noqa: F811
    auth_headers,
    fake_k8s,
):
    """Secret + link already moved; only the restart is pending -> distinct, retryable error."""
    created = await (await client.post(LLAMA, headers=auth_headers, json=_llama())).get_json()
    # only a live deployment has pods to roll (a pending one skips the restart)
    priced(priced.llamacpp_deployments.id == created["deployment_id"]).update(status="running")
    priced.commit()
    fake_k8s.fail_next_restart = True
    resp = await client.post(
        f"/api/v1/fleet/llamacpp/deployments/{created['public_id']}/rotate-token",
        headers=auth_headers,
    )
    body = await resp.get_json()
    assert resp.status_code == 502
    assert "restart pending" in body["error"] and body["reason"] == "restart pending"
    link = (
        priced(priced.connection_links.name == f"llamacpp-{created['public_id']}").select().first()
    )
    (secret_value,) = fake_k8s.secrets[fleet_secrets.DEPLOYMENT_KEYS_SECRET].values()
    assert decrypt_credential(link.api_key) == secret_value


async def test_llamacpp_rotate_link_failure_restores_old_secret(
    client,
    priced,  # noqa: F811
    auth_headers,
    fake_k8s,
    monkeypatch,  # noqa: F811
):
    """Link update fails right after the Secret write -> old Secret value restored, no restart."""
    created = await (await client.post(LLAMA, headers=auth_headers, json=_llama())).get_json()
    (old,) = fake_k8s.secrets[fleet_secrets.DEPLOYMENT_KEYS_SECRET].values()

    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr("services.management.app.api.v1.llamacpp._store_link_key", boom)
    resp = await client.post(
        f"/api/v1/fleet/llamacpp/deployments/{created['public_id']}/rotate-token",
        headers=auth_headers,
    )
    assert resp.status_code == 502 and fake_k8s.restart == {}
    assert list(fake_k8s.secrets[fleet_secrets.DEPLOYMENT_KEYS_SECRET].values()) == [old]


# --- 5: cluster-secret rotation reports partial application ----------------------------------


@pytest.fixture
def cluster(vllm_db, both_flags, fake_k8s, monkeypatch):
    """Ray secrets present, no external-source env vars, operator headers org."""
    for var in (
        "WADDLEAI_RAY_AUTH_TOKEN", "WADDLEAI_RAY_AUTH_TOKEN_EXISTING_SECRET",
        "WADDLEAI_RAY_TLS_CERT_PATH", "WADDLEAI_RAY_TLS_KEY_PATH", "WADDLEAI_RAY_TLS_CA_PATH",
        "WADDLEAI_RAY_TLS_EXISTING_SECRET",
    ):  # fmt: skip
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("WADDLEAI_SYSTEM_ORG_ID", "99")
    fake_k8s.secrets[fleet_secrets.RAY_AUTH_SECRET] = {"token": "OLD-token"}
    fake_k8s.secrets[fleet_secrets.RAY_TLS_SECRET] = {"tls.crt": "cert-body"}
    return fake_k8s


@pytest.fixture
def operator_headers(mint_token):
    """The platform operator token."""
    return {"Authorization": f"Bearer {mint_token(role='admin', org_id=99)}"}


async def test_secret_write_failed_flags_partial_application(cluster) -> None:
    """A restart failure AFTER the Secret changed carries partially_applied=True."""
    cluster.fail_next_restart = True
    with pytest.raises(fleet_secrets.SecretWriteFailed) as info:
        await fleet_secrets.rotate_secret("ray-auth-token")
    assert info.value.partially_applied is True
    assert cluster.secrets[fleet_secrets.RAY_AUTH_SECRET]["token"] != "OLD-token"  # noqa: S105
    cluster.fail_next_patch = True
    with pytest.raises(fleet_secrets.SecretWriteFailed) as clean:
        await fleet_secrets.rotate_secret("ray-auth-token")
    assert clean.value.partially_applied is False


async def test_rotate_api_surfaces_rotated_restart_pending_retry(
    client, cluster, operator_headers
) -> None:
    """The API tells the operator the Secret rotated but the restart is pending: retry."""
    cluster.fail_next_restart = True
    url = f"{SECRETS}/ray-auth-token/rotate"
    resp = await client.post(url, headers=operator_headers)
    body = await resp.get_json()
    assert resp.status_code == 502
    assert "rotated, restart pending, retry" in body["error"]
    assert (await client.post(url, headers=operator_headers)).status_code == 200  # retry allowed


async def test_tls_restart_waits_for_reissued_secret_and_times_out_loudly(
    cluster, monkeypatch
) -> None:
    """No restart until cert-manager's reissued Secret is readable; a timeout fails loudly."""
    monkeypatch.setattr(fleet_secrets, "_TLS_REISSUE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(fleet_secrets, "_TLS_REISSUE_POLL_S", 0.01)
    cluster.reissue_tls = False
    with pytest.raises(fleet_secrets.SecretWriteFailed) as info:
        await fleet_secrets.rotate_secret("ray-internal-tls")
    assert info.value.partially_applied is True and cluster.restart == {}
    cluster.reissue_tls = True
    await fleet_secrets.rotate_secret("ray-internal-tls")
    assert cluster.restart == {"RayCluster/waddleai-vllm-ray": 1}


# --- 8: HF token Secret never leaks ----------------------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A model-store root and a fake Kubernetes."""
    root = tmp_path / "models"
    root.mkdir()
    monkeypatch.setenv("WADDLEAI_MODEL_STORE_PATH", str(root))
    monkeypatch.setenv("WADDLEAI_MODEL_PULL_IMAGE", "registry/management@sha256:abc")
    k8s = FakeK8s()
    fleet_secrets.set_gateway(k8s)
    yield k8s
    fleet_secrets.set_gateway(None)


def test_hf_secret_deleted_when_create_job_fails(vllm_db, store) -> None:
    """The short-lived HF token Secret is removed if the pull Job cannot be created."""
    from shared.security.credential_encryption import encrypt_credential

    backend = seed_backend(vllm_db)
    vllm_db(vllm_db.fleet_backends.id == backend).update(
        credentials_ref=encrypt_credential("hf_tok")
    )
    vllm_db.commit()
    dep = seed_deployment(vllm_db, backend, status=1)
    store.fail_create_job = True
    with pytest.raises(RuntimeError):
        asyncio.run(
            model_store.ensure_weights(vllm_db, 1, "org/Model-7B", dep.id, 1, slug=dep.slug)
        )
    assert f"waddleai-hf-{dep.id}" not in store.secrets


async def test_delete_deployment_removes_hf_secret(
    client, ready, auth_headers, fake_k8s, vllm_db
) -> None:
    """DELETE also drops a leftover waddleai-hf-<id> Secret."""
    dep = await _dep(client, auth_headers, ready)
    row = vllm_db(vllm_db.vllm_deployments.slug == dep["slug"]).select().first()
    fake_k8s.secrets[f"waddleai-hf-{row.id}"] = {"HF_TOKEN": "x"}
    resp = await client.delete(f"{DEPS}/{dep['public_id']}", headers=auth_headers)
    assert resp.status_code == 200
    assert f"waddleai-hf-{row.id}" not in fake_k8s.secrets


# --- 9: create never orphans a rate row ------------------------------------------------------


def _vllm_rates(db) -> int:
    return db(db.token_conversion_rates.provider == "vllm").count()


async def test_name_conflict_creates_no_orphan_rate_row(client, ready, auth_headers, vllm_db):
    """A 409 name conflict leaves no vllm:deployment/<uuid> rate row behind."""
    await _dep(client, auth_headers, ready)
    assert _vllm_rates(vllm_db) == 1
    resp = await client.post(DEPS, headers=auth_headers, json=_body(ready))
    assert resp.status_code == 409 and _vllm_rates(vllm_db) == 1


async def test_name_conflict_race_discards_the_rate_row(
    client, ready, auth_headers, vllm_db, monkeypatch
):
    """Pre-check passes but the insert loses the race: 409 and the rate row is discarded."""
    await _dep(client, auth_headers, ready)
    monkeypatch.setattr(f"{FV}._name_taken", lambda *_a: False)
    resp = await client.post(DEPS, headers=auth_headers, json=_body(ready))
    assert resp.status_code == 409 and _vllm_rates(vllm_db) == 1


# --- 10: no cross-org existence oracle (helpers) ---------------------------------------------


async def test_cross_org_deployment_and_backend_are_indistinguishable_from_missing(
    client, ready, vllm_db, auth_headers
):
    """Every deployment verb answers a foreign id exactly like an unknown id (404, same body)."""
    other = seed_backend(vllm_db, org_id=2, name="o2")
    foreign = seed_deployment(vllm_db, other, org_id=2, name="f")
    for method, suffix in (
        ("get", ""), ("patch", ""), ("delete", ""), ("get", "/events"), ("post", "/rotate-token")
    ):  # fmt: skip
        kw = {"json": {}} if method in ("patch", "post") else {}
        f = await getattr(client, method)(
            f"{DEPS}/{foreign.public_id}{suffix}", headers=auth_headers, **kw
        )
        m = await getattr(client, method)(
            f"{DEPS}/{uuid.uuid4()}{suffix}", headers=auth_headers, **kw
        )
        assert f.status_code == m.status_code == 404, (method, suffix)
        assert await f.get_json() == await m.get_json()
