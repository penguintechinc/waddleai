"""Regressions for the llama.cpp lifecycle review (name scope, rotate kind/lease, delete/remove)."""
# ruff: noqa: F401, F811  (pytest fixtures imported by name)

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from services.management.app.api.v1 import llamacpp as api
from services.management.app.services import fleet_secrets
from services.management.app.services.llamacpp_manager import LlamaCppManager
from tests.unit.management.test_llamacpp_rpc_routes import URL as RPC_URL
from tests.unit.management.test_llamacpp_rpc_routes import _body, org2_headers, rpc
from tests.unit.management.test_local_pricing_api import LLAMA, _llama, priced
from tests.unit.management.vllm_harness import seed_backend, seed_gpus

ROTATE = "/api/v1/fleet/llamacpp/deployments/{}/rotate-token"


async def _create(client, headers, **over):
    """Create a single-node deployment, asserting 201; returns the body."""
    resp = await client.post(LLAMA, headers=headers, json=_llama(**over))
    assert resp.status_code == 201, await resp.get_json()
    return await resp.get_json()


def _row(db, dep_id):
    """The llamacpp_deployments row."""
    return db(db.llamacpp_deployments.id == dep_id).select().first()


def _set_status(db, dep_id, status):
    """Force a row status."""
    db(db.llamacpp_deployments.id == dep_id).update(status=status)
    db.commit()


def _link(db, public_id):
    """The deployment's key link."""
    return db(db.connection_links.name == f"llamacpp-{public_id}").select().first()


async def test_same_org_duplicate_name_is_409_with_generic_message(client, priced, auth_headers):  # noqa: F811
    """regression: duplicate name was a 500; now 409 and the body never echoes the name."""
    await _create(client, auth_headers, name="squat-me")
    resp = await client.post(LLAMA, headers=auth_headers, json=_llama(name="squat-me"))
    assert resp.status_code == 409
    assert "squat-me" not in (await resp.get_json())["error"]
    assert priced(priced.llamacpp_deployments.name == "squat-me").count() == 1


async def test_other_org_may_reuse_a_name_without_colliding_in_k8s(
    client,
    priced,  # noqa: F811
    auth_headers,
    org2_headers,  # noqa: F811
):
    """regression: names were globally unique (cross-tenant probe); now per owner org."""
    backend2 = seed_backend(priced, org_id=2, name="gpu2")
    seed_gpus(priced, backend2, org_id=2, count=1)
    one = await _create(client, auth_headers, name="shared-name")
    two = await _create(client, org2_headers, name="shared-name")
    first, second = _row(priced, one["deployment_id"]), _row(priced, two["deployment_id"])
    assert (first.owner_org_id, second.owner_org_id) == (1, 2)
    assert first.k8s_daemonset_name != second.k8s_daemonset_name
    for row in (first, second):
        assert str(row.public_id).replace("-", "")[:12] in row.k8s_daemonset_name


async def test_rpc_same_org_duplicate_name_is_409_and_cross_org_is_not(
    client,
    rpc,  # noqa: F811
    auth_headers,
    org2_headers,  # noqa: F811
):
    """The RPC create path applies the same per-org rule (before any planning or K8s call)."""
    assert (await client.post(RPC_URL, headers=auth_headers, json=_body())).status_code == 201
    dup = await client.post(RPC_URL, headers=auth_headers, json=_body())
    assert dup.status_code == 409 and "qwen-rpc" not in (await dup.get_json())["error"]
    assert rpc.deploy.call_count == 1


async def test_single_node_rotate_restarts_the_daemonset(client, priced, auth_headers, fake_k8s):  # noqa: F811
    """regression: rotate bumped a StatefulSet; single-node deployments are DaemonSets."""
    created = await _create(client, auth_headers)
    _set_status(priced, created["deployment_id"], "running")
    name = _row(priced, created["deployment_id"]).k8s_daemonset_name
    resp = await client.post(ROTATE.format(created["public_id"]), headers=auth_headers)
    assert resp.status_code == 200
    assert fake_k8s.restart == {f"DaemonSet/{name}": 1}


async def test_rotate_of_a_stopped_deployment_skips_the_restart(
    client, priced, auth_headers, fake_k8s
):  # noqa: F811
    """M4: nothing is running, so there is no pod to roll; the Secret still rotates."""
    created = await _create(client, auth_headers)
    before = dict(fake_k8s.secrets[fleet_secrets.DEPLOYMENT_KEYS_SECRET])
    resp = await client.post(ROTATE.format(created["public_id"]), headers=auth_headers)
    assert resp.status_code == 200 and fake_k8s.restart == {}
    assert fake_k8s.secrets[fleet_secrets.DEPLOYMENT_KEYS_SECRET] != before


async def test_rotation_is_single_flight_per_deployment(
    client, priced, auth_headers, fake_k8s, monkeypatch
):  # noqa: F811
    """M4: a held lease turns a concurrent rotation into 409 with Secret and link untouched."""
    created = await _create(client, auth_headers)
    dep_id = created["deployment_id"]
    secret_before = dict(fake_k8s.secrets[fleet_secrets.DEPLOYMENT_KEYS_SECRET])
    link_before = _link(priced, created["public_id"]).api_key
    priced(priced.llamacpp_deployments.id == dep_id).update(
        rotation_lease_until=datetime.utcnow() + timedelta(seconds=30)
    )
    priced.commit()
    busy = await client.post(ROTATE.format(created["public_id"]), headers=auth_headers)
    assert busy.status_code == 409
    assert fake_k8s.secrets[fleet_secrets.DEPLOYMENT_KEYS_SECRET] == secret_before
    assert _link(priced, created["public_id"]).api_key == link_before
    # an EXPIRED lease (crashed holder) does not wedge rotation forever, and is released after
    priced(priced.llamacpp_deployments.id == dep_id).update(
        rotation_lease_until=datetime.utcnow() - timedelta(seconds=1)
    )
    priced.commit()
    ok = await client.post(ROTATE.format(created["public_id"]), headers=auth_headers)
    assert ok.status_code == 200 and _row(priced, dep_id).rotation_lease_until is None


def test_lease_acquire_is_exclusive_until_released(priced):  # noqa: F811
    """The conditional UPDATE hands the lease to exactly one caller."""
    dep_id = priced.llamacpp_deployments.insert(
        name="lease", deployment_type="kubernetes", status="stopped", model_name="m7b",
        owner_org_id=1, public_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
    )  # fmt: skip
    priced.commit()
    assert api._acquire_rotation_lease(dep_id) is True
    assert api._acquire_rotation_lease(dep_id) is False
    api._release_rotation_lease(dep_id)
    assert api._acquire_rotation_lease(dep_id) is True


async def test_force_delete_failure_keeps_row_key_and_link(
    client,
    priced,  # noqa: F811
    auth_headers,
    fake_k8s,
    monkeypatch,
):
    """H1: a removal that fails must not delete the row/key/link (it would orphan objects)."""
    created = await _create(client, auth_headers)
    _set_status(priced, created["deployment_id"], "running")

    def boom(self, dep, force=False):
        raise RuntimeError("apiserver down")

    monkeypatch.setattr(LlamaCppManager, "remove_daemonset", boom)
    resp = await client.delete(
        f"{LLAMA}/{created['deployment_id']}?force=true", headers=auth_headers
    )
    assert resp.status_code == 502 and "apiserver" not in (await resp.get_json())["error"]
    assert _row(priced, created["deployment_id"]) is not None
    assert _link(priced, created["public_id"]) is not None
    assert fake_k8s.secrets[fleet_secrets.DEPLOYMENT_KEYS_SECRET]  # key still present


async def test_remove_disables_the_link_and_hides_errors(
    client,
    priced,  # noqa: F811
    auth_headers,
    monkeypatch,
):
    """M6: /remove stops routing in the same step; a failure returns a generic message."""
    created = await _create(client, auth_headers)
    dep_id = created["deployment_id"]
    priced(priced.connection_links.name == f"llamacpp-{created['public_id']}").update(enabled=True)
    priced.commit()
    monkeypatch.setattr(LlamaCppManager, "remove_daemonset", lambda self, dep, force=False: None)
    ok = await client.post(f"{LLAMA}/{dep_id}/remove", headers=auth_headers)
    assert ok.status_code == 200
    assert _link(priced, created["public_id"]).enabled is False

    priced(priced.connection_links.name == f"llamacpp-{created['public_id']}").update(enabled=True)
    priced.commit()

    def boom(self, dep, force=False):
        raise RuntimeError("secret internal detail 10.0.0.7")

    monkeypatch.setattr(LlamaCppManager, "remove_daemonset", boom)
    bad = await client.post(f"{LLAMA}/{dep_id}/remove", headers=auth_headers)
    assert bad.status_code == 503 and "10.0.0.7" not in (await bad.get_json())["error"]
    assert _link(priced, created["public_id"]).enabled is True  # not stopped: keep routing


async def test_deploy_of_a_row_already_deploying_is_409(client, priced, auth_headers, monkeypatch):  # noqa: F811
    """M3: deploy claims the row atomically; a second deploy never creates objects twice."""
    created = await _create(client, auth_headers)
    calls: list[int] = []
    monkeypatch.setattr(LlamaCppManager, "deploy_daemonset", lambda self, dep: calls.append(1))
    _set_status(priced, created["deployment_id"], "deploying")
    resp = await client.post(f"{LLAMA}/{created['deployment_id']}/deploy", headers=auth_headers)
    assert resp.status_code == 409 and calls == []


async def test_failed_deploy_releases_the_claim(client, priced, auth_headers, monkeypatch):  # noqa: F811
    """M3: a deploy that errors puts the row back so it can be retried."""
    created = await _create(client, auth_headers)

    def boom(self, dep):
        raise RuntimeError("quota")

    monkeypatch.setattr(LlamaCppManager, "deploy_daemonset", boom)
    resp = await client.post(f"{LLAMA}/{created['deployment_id']}/deploy", headers=auth_headers)
    assert resp.status_code == 503
    assert _row(priced, created["deployment_id"]).status == "pending"


@pytest.mark.parametrize("status", ["running", "deploying", "degraded"])
async def test_live_rpc_row_cannot_be_deployed_again(client, rpc, auth_headers, status):  # noqa: F811
    """M3: the RPC redeploy claim refuses a live row without touching Kubernetes."""
    created = await (await client.post(RPC_URL, headers=auth_headers, json=_body())).get_json()
    _set_status(rpc.db, created["deployment_id"], status)
    rpc.deploy.reset_mock()
    resp = await client.post(f"{RPC_URL}/{created['deployment_id']}/deploy", headers=auth_headers)
    assert resp.status_code == 409 and rpc.deploy.call_count == 0
