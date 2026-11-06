"""C2: vLLM deployment CRUD, pricing modes, token minting, events and DTO shapes."""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from services.management.app.services import fleet_secrets
from shared.security.credential_encryption import decrypt_credential
from tests.unit.management.vllm_harness import (
    seed_backend,
    seed_deployment,
    seed_gpus,
    seed_rate,
)

DEPS = "/api/v1/fleet/vllm/deployments"


def _body(backend: int, **over):
    """A valid create body."""
    body = {
        "name": "dep1",
        "model_ref": "org/Model-7B",
        "fleet_backend_id": backend,
        "vendor": 1,
        "gpu_class": "nvidia-rtx4090",
        "tensor_parallel": 1,
        "pipeline_parallel": 1,
        "replicas_min": 1,
        "replicas_max": 1,
        "comparison_model": "openai/gpt-4o",
    }
    body.update(over)
    return body


@pytest.fixture
def ready(vllm_db, both_flags, fake_backend, fake_k8s, license_tier, monkeypatch):
    """Backend + warm GPUs + a commercial rate; weights 'present' so create deploys at once."""
    backend = seed_backend(vllm_db)
    seed_gpus(vllm_db, backend, count=4)
    seed_rate(vllm_db)
    monkeypatch.setattr(
        "services.management.app.api.v1.fleet_vllm.model_store.ensure_weights",
        AsyncMock(return_value="/models/1/org/Model-7B"),
    )
    return backend


async def _create(client, headers, backend, **over):
    return await client.post(DEPS, headers=headers, json=_body(backend, **over))


async def test_create_happy_path_201_pending_deploy_called_once(
    client, ready, auth_headers, fake_backend, vllm_db, fake_k8s
):
    """201; deploy once; token minted into the Secret and the link; slug from derive_slug."""
    resp = await _create(client, auth_headers, ready)
    assert resp.status_code == 201
    data = (await resp.get_json())["data"]
    assert data["status"] == 1 and data["price_mode"] == 1
    assert [c for c in fake_backend.calls if c[0] == "deploy"] == [("deploy", data["slug"])]
    assert data["slug"].endswith(uuid.UUID(data["public_id"]).hex[:12])
    token = fake_k8s.deployment_key(data["slug"])
    link = vllm_db(vllm_db.connection_links.id > 0).select().first()
    assert link.enabled is False and link.owner_org_id == 1
    assert decrypt_credential(link.api_key) == token != link.api_key
    assert token not in str(data) and data["api_key_fingerprint"] == fleet_secrets.fingerprint(
        token
    )
    rate = vllm_db(vllm_db.token_conversion_rates.provider == "vllm").select().first()
    assert rate.model == f"deployment/{data['public_id']}"


async def test_create_denied_origin_403_before_fit_or_deploy(
    client, ready, auth_headers, fake_backend
):
    """A blocked publisher is 403 and nothing is deployed."""
    resp = await _create(client, auth_headers, ready, model_ref="deepseek-ai/DeepSeek-7B")
    assert resp.status_code == 403
    assert "blocked-origin" in (await resp.get_json())["error"]
    assert fake_backend.calls == []


async def test_create_quant_not_in_matrix_422(client, vllm_db, ready, auth_headers):
    """AWQ on an Intel vendor names the allowed options."""
    resp = await _create(client, auth_headers, ready, vendor=3, quantization=1)
    assert resp.status_code == 422
    assert "Allowed options" in (await resp.get_json())["error"]


async def test_create_free_cap_exceeded_403_upgrade_text(
    client, ready, auth_headers, license_tier, vllm_db
):
    """Free tier at its registered-model cap: 403 with upgrade text."""
    license_tier("community")
    for i in range(3):
        seed_deployment(
            vllm_db,
            ready,
            name=f"x{i}",
            model_ref=f"o/M{i}-1B",
            served_model_name=f"m{i}",
            status=5,
        )
    resp = await _create(client, auth_headers, ready, name="fourth", model_ref="o/Other-7B")
    assert resp.status_code == 403
    assert "Upgrade" in (await resp.get_json())["error"]


async def test_create_does_not_fit_422_with_refusal_verbatim(
    client, ready, auth_headers, fake_backend
):
    """No fit: 422 carries the Plan B refusal text; nothing deployed or inserted."""
    resp = await _create(client, auth_headers, ready, model_ref="org/Giant-4000B")
    assert resp.status_code == 422
    assert (await resp.get_json())["error"].startswith("Does not fit")
    assert fake_backend.calls == []


async def test_create_refusal_none_uses_fallback_text(client, ready, auth_headers, monkeypatch):
    """A fit result with no refusal text falls back to a plain sentence."""
    from shared.fleet.vllm_ray.fit import FitResult

    monkeypatch.setattr(
        "services.management.app.api.v1.fleet_vllm.fit_check",
        lambda *a, **k: FitResult(False, [], None),
    )
    resp = await _create(client, auth_headers, ready)
    assert resp.status_code == 422
    assert (await resp.get_json())[
        "error"
    ] == "The fit check did not explain why. Contact your administrator."


@pytest.mark.parametrize(
    "prices",
    [
        {},
        {"price_input_per_1m": "0", "price_output_per_1m": "1"},
        {"price_input_per_1m": "-1", "price_output_per_1m": "1"},
        {"price_input_per_1m": "nan", "price_output_per_1m": "1"},
        {"price_input_per_1m": "abc", "price_output_per_1m": "1"},
    ],
)
async def test_create_manual_requires_positive_price(client, ready, auth_headers, prices):
    """MANUAL mode needs finite prices > 0."""
    resp = await _create(client, auth_headers, ready, price_mode=2, **prices)
    assert resp.status_code == 400


async def test_create_manual_price_is_stored(client, ready, auth_headers, vllm_db):
    """MANUAL prices are persisted and a rate row is upserted."""
    resp = await _create(
        client,
        auth_headers,
        ready,
        price_mode=2,
        price_input_per_1m="2.5",
        price_output_per_1m="7.5",
    )
    data = (await resp.get_json())["data"]
    assert (data["price_mode"], Decimal(data["price_input_per_1m"])) == (2, Decimal("2.5"))
    assert vllm_db(vllm_db.token_conversion_rates.provider == "vllm").count() == 1


async def test_create_auto_ignores_client_price(client, ready, auth_headers, vllm_db):
    """AUTO ignores a client price; the stored price is the TCO result; breakdown persisted."""
    resp = await _create(
        client, auth_headers, ready, price_input_per_1m="9.99", price_output_per_1m="9.99"
    )
    data = (await resp.get_json())["data"]
    assert Decimal(data["price_output_per_1m"]) != Decimal("9.99")
    assert data["price_breakdown"]["gpu_class"] == "nvidia-rtx4090"
    assert data["price_breakdown"]["params_b"] == 7.0


async def test_create_invalid_price_mode_400(client, ready, auth_headers):
    """price_mode outside {1,2} is 400."""
    assert (await _create(client, auth_headers, ready, price_mode=3)).status_code == 400


async def test_create_expose_and_prefer_local_persisted(client, ready, auth_headers):
    """expose_via_proxy defaults true; prefer_local is stored."""
    default = (await (await _create(client, auth_headers, ready)).get_json())["data"]
    assert default["expose_via_proxy"] is True and default["prefer_local"] is None
    custom = (
        await (
            await _create(
                client, auth_headers, ready, name="b", expose_via_proxy=False,
                prefer_local={"fallback_model": "gpt-4o", "match": {"team": "x"}},
            )
        ).get_json()
    )["data"]  # fmt: skip
    assert (
        custom["expose_via_proxy"] is False and custom["prefer_local"]["fallback_model"] == "gpt-4o"
    )


async def test_create_comparison_model_without_rate_422(client, ready, auth_headers):
    """comparison_model must have an enabled commercial rate row."""
    resp = await _create(client, auth_headers, ready, comparison_model="openai/none")
    assert resp.status_code == 422
    missing = _body(ready)
    del missing["comparison_model"]
    assert (await client.post(DEPS, headers=auth_headers, json=missing)).status_code == 400


async def test_create_same_name_same_org_409(client, ready, auth_headers):
    """Twice in one org: 409."""
    assert (await _create(client, auth_headers, ready)).status_code == 201
    assert (await _create(client, auth_headers, ready)).status_code == 409


async def test_create_same_name_different_org_ok(client, ready, vllm_db, mint_token):
    """Same name in two orgs is fine (distinct slugs)."""
    other = seed_backend(vllm_db, org_id=2, name="o2")
    seed_gpus(vllm_db, other, org_id=2, count=4)
    h1 = {"Authorization": f"Bearer {mint_token(role='admin', org_id=1)}"}
    h2 = {"Authorization": f"Bearer {mint_token(role='admin', org_id=2)}"}
    a = await _create(client, h1, ready)
    b = await _create(client, h2, other)
    assert (a.status_code, b.status_code) == (201, 201)
    assert (await a.get_json())["data"]["slug"] != (await b.get_json())["data"]["slug"]


async def test_create_served_name_equal_to_commercial_model_ok(
    client, ready, auth_headers, vllm_db
):
    """A served name equal to a commercial model is allowed and writes no model_configs."""
    resp = await _create(client, auth_headers, ready, served_model_name="gpt-4o")
    assert resp.status_code == 201
    assert vllm_db(vllm_db.model_configs.id > 0).count() == 0


async def test_create_backend_foreign_org_403_and_missing_404(client, ready, vllm_db, auth_headers):
    """Another org's backend is 403; a missing one is 404; a non-vllm backend is 400."""
    foreign = seed_backend(vllm_db, org_id=2, name="f")
    assert (await _create(client, auth_headers, foreign)).status_code == 403
    assert (await _create(client, auth_headers, 9999)).status_code == 404
    vllm_db(vllm_db.fleet_backends.id == ready).update(type="ollama")
    vllm_db.commit()
    assert (await _create(client, auth_headers, ready)).status_code == 400


async def test_create_backend_deploy_error_marks_failed_502(
    client, ready, auth_headers, fake_backend, vllm_db
):
    """A deploy failure is 502 with the backend's detail; the secret write already happened."""
    fake_backend.deploy_error = "Ray Serve rejected the deployment; see deployment events."
    resp = await _create(client, auth_headers, ready)
    assert resp.status_code == 502
    assert "rejected" in (await resp.get_json())["error"]


async def test_create_secret_write_failure_rolls_back_row(
    client, ready, auth_headers, fake_k8s, vllm_db
):
    """If the key Secret cannot be written the deployment row and its rate row are removed."""
    fake_k8s.fail_next_patch = True
    resp = await _create(client, auth_headers, ready)
    assert resp.status_code == 502
    assert vllm_db(vllm_db.vllm_deployments.id > 0).count() == 0
    assert vllm_db(vllm_db.token_conversion_rates.provider == "vllm").count() == 0


async def test_create_while_pulling_does_not_deploy(
    client, ready, auth_headers, fake_backend, vllm_db, monkeypatch
):
    """When the weights are being pulled the row is PULLING and deploy waits for the reconciler."""

    async def pulling(db, org, ref, dep_id, kind, slug=None):
        db(db.vllm_deployments.id == dep_id).update(status=2)
        db.commit()
        return "/models/x"

    monkeypatch.setattr(
        "services.management.app.api.v1.fleet_vllm.model_store.ensure_weights", pulling
    )
    resp = await _create(client, auth_headers, ready)
    assert resp.status_code == 201 and (await resp.get_json())["data"]["status"] == 2
    assert fake_backend.calls == []


@pytest.fixture
def three(vllm_db, both_flags):
    """Three deployments in org 1 and one in org 2."""
    backend = seed_backend(vllm_db)
    other = seed_backend(vllm_db, org_id=2, name="o2")
    seed_deployment(vllm_db, backend, name="a", status=5, model_ref="org/Alpha-7B")
    seed_deployment(vllm_db, backend, name="b", status=7, vendor=2, model_ref="org/Beta-7B")
    seed_deployment(vllm_db, backend, name="c", status=5, model_ref="org/Gamma-7B")
    seed_deployment(vllm_db, other, org_id=2, name="z", status=5)
    return backend


@pytest.mark.parametrize(
    ("query", "names"),
    [
        ("", ["a", "b", "c"]),
        ("status=5", ["a", "c"]),
        ("vendor=2", ["b"]),
        ("model=beta", ["b"]),
        ("status=1", []),
    ],
)
async def test_list_filters_org_scoped_and_empty(client, three, auth_headers, query, names):
    """Filters narrow, other orgs never leak, an empty result is a 200."""
    body = await (await client.get(f"{DEPS}?{query}", headers=auth_headers)).get_json()
    assert [d["name"] for d in body["data"]] == names
    assert body["meta"]["total"] == len(names)


async def test_list_pagination_and_invalid_filters(client, three, auth_headers):
    """page/limit window the list; invalid filter codes are 400."""
    body = await (await client.get(f"{DEPS}?page=2&limit=2", headers=auth_headers)).get_json()
    assert [d["name"] for d in body["data"]] == ["c"] and body["meta"]["page"] == 2
    for bad in ("status=99", "status=abc", "vendor=7"):
        assert (await client.get(f"{DEPS}?{bad}", headers=auth_headers)).status_code == 400


async def test_detail_has_timeline_404_unknown_403_foreign_org(
    client, three, vllm_db, auth_headers
):
    """Detail carries events; unknown 404; another org's 403."""
    dep = vllm_db(vllm_db.vllm_deployments.name == "a").select().first()
    vllm_db.vllm_deployment_events.insert(
        deployment_id=dep.id, ts=dep.created_at, state=5, message="serving"
    )
    vllm_db.commit()
    body = (await (await client.get(f"{DEPS}/{dep.public_id}", headers=auth_headers)).get_json())[
        "data"
    ]
    assert body["events"][0]["message"] == "serving"
    assert (await client.get(f"{DEPS}/{uuid.uuid4()}", headers=auth_headers)).status_code == 404
    foreign = vllm_db(vllm_db.vllm_deployments.name == "z").select().first()
    assert (
        await client.get(f"{DEPS}/{foreign.public_id}", headers=auth_headers)
    ).status_code == 403


async def test_events_ordered_desc_paginated_org_scoped(client, three, vllm_db, auth_headers):
    """Events are newest first, paginated, and org scoped."""
    from datetime import datetime, timedelta

    dep = vllm_db(vllm_db.vllm_deployments.name == "a").select().first()
    base = datetime(2026, 1, 1)
    for i in range(3):
        vllm_db.vllm_deployment_events.insert(
            deployment_id=dep.id, ts=base + timedelta(minutes=i), state=3, message=f"m{i}"
        )
    vllm_db.commit()
    url = f"{DEPS}/{dep.public_id}/events"
    body = await (await client.get(f"{url}?page=1&limit=2", headers=auth_headers)).get_json()
    assert [e["message"] for e in body["data"]] == ["m2", "m1"] and body["meta"]["total"] == 3
    page2 = await (await client.get(f"{url}?page=2&limit=2", headers=auth_headers)).get_json()
    assert [e["message"] for e in page2["data"]] == ["m0"]
    foreign = vllm_db(vllm_db.vllm_deployments.name == "z").select().first()
    assert (
        await client.get(f"{DEPS}/{foreign.public_id}/events", headers=auth_headers)
    ).status_code == 403


def _first(db, name="a"):
    return db(db.vllm_deployments.name == name).select().first()


async def test_patch_replicas_calls_scale_and_validates(
    client, three, vllm_db, auth_headers, fake_backend
):
    """Replica bounds go to backend.scale; max below min is 400."""
    dep = _first(vllm_db)
    ok = await client.patch(
        f"{DEPS}/{dep.public_id}", headers=auth_headers, json={"replicas_min": 1, "replicas_max": 3}
    )
    assert ok.status_code == 200 and ("scale", (1, 3)) in fake_backend.calls
    bad = await client.patch(
        f"{DEPS}/{dep.public_id}", headers=auth_headers, json={"replicas_min": 5, "replicas_max": 2}
    )
    assert bad.status_code == 400


async def test_patch_stop_then_resume_and_conflicts(
    client, three, vllm_db, auth_headers, fake_backend
):
    """Stop calls backend.stop; stop/resume conflicts are 409; resume of STOPPED deploys."""
    dep = _first(vllm_db, "b")  # STOPPED
    url = f"{DEPS}/{dep.public_id}"
    assert (
        await client.patch(url, headers=auth_headers, json={"action": "stop"})
    ).status_code == 409
    assert (
        await client.patch(url, headers=auth_headers, json={"action": "resume"})
    ).status_code == 200
    assert ("deploy", dep.slug) in fake_backend.calls
    live = _first(vllm_db, "a")
    assert (
        await client.patch(
            f"{DEPS}/{live.public_id}", headers=auth_headers, json={"action": "resume"}
        )
    ).status_code == 409
    assert (
        await client.patch(
            f"{DEPS}/{live.public_id}", headers=auth_headers, json={"action": "stop"}
        )
    ).status_code == 200
    assert ("stop", live.slug) in fake_backend.calls
    assert (
        await client.patch(url, headers=auth_headers, json={"action": "bogus"})
    ).status_code == 400


async def test_patch_price_updates_row_only_and_validates_positive(
    client, three, vllm_db, auth_headers, fake_backend
):
    """A manual price update touches only the row (backend untouched); non-positive is 400."""
    dep = _first(vllm_db)
    url = f"{DEPS}/{dep.public_id}"
    ok = await client.patch(
        url,
        headers=auth_headers,
        json={"price_mode": 2, "price_input_per_1m": "3", "price_output_per_1m": "9"},
    )
    assert ok.status_code == 200
    data = (await ok.get_json())["data"]
    assert (data["price_mode"], Decimal(data["price_output_per_1m"])) == (2, Decimal("9"))
    assert fake_backend.calls == []
    for bad in (
        {"price_mode": 2, "price_input_per_1m": "0", "price_output_per_1m": "1"},
        {"price_mode": 2},
        {"price_mode": 2, "price_input_per_1m": "nan", "price_output_per_1m": "1"},
    ):
        assert (await client.patch(url, headers=auth_headers, json=bad)).status_code == 400


async def test_patch_back_to_auto_recomputes(
    client, vllm_db, both_flags, auth_headers, fake_backend
):
    """Switching MANUAL back to AUTO recomputes via TCO and refreshes the breakdown."""
    backend = seed_backend(vllm_db)
    dep = seed_deployment(
        vllm_db, backend, price_mode=2, price_input_per_1m="50", price_output_per_1m="90"
    )
    resp = await client.patch(
        f"{DEPS}/{dep.public_id}", headers=auth_headers, json={"price_mode": 1}
    )
    data = (await resp.get_json())["data"]
    assert data["price_mode"] == 1 and Decimal(data["price_output_per_1m"]) != Decimal("90")
    assert data["price_breakdown"]["throughput_source"] == "estimate"


async def test_delete_orders_backend_delete_then_remove_key_then_unlink_then_row(
    client, three, vllm_db, auth_headers, fake_backend, fake_k8s, monkeypatch
):
    """DELETE order: backend.delete, remove_key(slug), unlink, row delete; 200 deleted."""
    order: list[str] = []
    dep = _first(vllm_db)
    fake_k8s.secrets[fleet_secrets.DEPLOYMENT_KEYS_SECRET] = {dep.slug: "tok"}
    real_delete = fake_backend.delete

    async def delete(d):
        order.append("backend.delete")
        await real_delete(d)

    async def remove_key(slug):
        order.append(f"remove_key:{slug}")

    async def unlink(db, d):
        order.append("unlink")

    monkeypatch.setattr(fake_backend, "delete", delete)
    monkeypatch.setattr(
        "services.management.app.api.v1.fleet_vllm.fleet_secrets.remove_key", remove_key
    )
    monkeypatch.setattr("services.management.app.api.v1.fleet_vllm.unlink_deployment", unlink)
    resp = await client.delete(f"{DEPS}/{dep.public_id}", headers=auth_headers)
    assert resp.status_code == 200 and (await resp.get_json())["data"] == {"deleted": True}
    assert order == ["backend.delete", f"remove_key:{dep.slug}", "unlink"]
    assert _first(vllm_db) is None


async def test_delete_failure_keeps_row_and_foreign_is_403(
    client, three, vllm_db, auth_headers, fake_backend
):
    """A backend failure leaves the row for retry (502); another org's deployment is 403."""
    dep = _first(vllm_db)
    fake_backend.delete_error = "Ray Serve rejected the request; see backend logs."
    assert (await client.delete(f"{DEPS}/{dep.public_id}", headers=auth_headers)).status_code == 502
    assert _first(vllm_db) is not None
    foreign = _first(vllm_db, "z")
    assert (
        await client.delete(f"{DEPS}/{foreign.public_id}", headers=auth_headers)
    ).status_code == 403


async def test_every_route_404_when_second_flag_off(client, vllm_db, auth_headers, monkeypatch):
    """With only fleet_v2 on, every vLLM route is 404 not_found (flags excepted)."""
    monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "1")
    pid = uuid.uuid4()
    calls = [
        ("get", "/api/v1/fleet/gpu-inventory"), ("get", "/api/v1/fleet/vllm/models"),
        ("post", "/api/v1/fleet/vllm/fit-check"), ("get", "/api/v1/fleet/price-suggestion"),
        ("get", DEPS), ("post", DEPS), ("get", f"{DEPS}/{pid}"), ("patch", f"{DEPS}/{pid}"),
        ("delete", f"{DEPS}/{pid}"), ("get", f"{DEPS}/{pid}/events"),
        ("post", f"{DEPS}/{pid}/rotate-token"), ("delete", "/api/v1/fleet/models/org/m/weights"),
    ]  # fmt: skip
    for method, path in calls:
        resp = await getattr(client, method)(
            path, headers=auth_headers, **({"json": {}} if method in ("post", "patch") else {})
        )
        assert resp.status_code == 404, (method, path)
        assert (await resp.get_json())["error"] == "not_found"


async def test_every_route_401_without_jwt_and_403_for_non_admin(
    client, vllm_db, both_flags, user_auth_headers
):
    """No JWT: 401; a non-fleet-admin: 403 (a sample across all route shapes)."""
    pid = uuid.uuid4()
    for method, path in [
        ("get", DEPS),
        ("post", DEPS),
        ("patch", f"{DEPS}/{pid}"),
        ("delete", f"{DEPS}/{pid}"),
        ("post", f"{DEPS}/{pid}/rotate-token"),
        ("get", "/api/v1/fleet/secrets"),
        ("delete", "/api/v1/fleet/models/org/m/weights"),
        ("get", "/api/v1/fleet/vllm/models"),
    ]:
        extra = {"json": {}} if method in ("post", "patch") else {}
        assert (await getattr(client, method)(path, **extra)).status_code == 401, path
        assert (
            await getattr(client, method)(path, headers=user_auth_headers, **extra)
        ).status_code == 403, path


async def test_dtos_have_exact_field_sets(client, three, vllm_db, auth_headers):
    """Responses carry exactly the DTO fields: no numeric id, no token."""
    from dataclasses import fields

    from services.management.app.api.v1.fleet_vllm import DeploymentDetail, DeploymentItem

    dep = _first(vllm_db)
    listed = (await (await client.get(DEPS, headers=auth_headers)).get_json())["data"][0]
    assert set(listed) == {f.name for f in fields(DeploymentItem)} and "id" not in listed
    detail = (await (await client.get(f"{DEPS}/{dep.public_id}", headers=auth_headers)).get_json())[
        "data"
    ]
    assert set(detail) == {f.name for f in fields(DeploymentDetail)}
