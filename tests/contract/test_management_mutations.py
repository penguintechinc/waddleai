"""
Regression tests for validation-ordering fixes in the management service's
Flask -> Quart migration (branch chore/consolidate-quart-k8s).

The async conversion hoisted pure-Python field validation ahead of DB
existence/conflict checks that ran FIRST in the pre-migration (Flask)
originals (commit 44cc384). When a request trips two failure conditions at
once, the wrong status/body was returned. These are targeted assertion
tests (not snapshots) -- the GET-only contract snapshots in
test_management_contract.py do not exercise these dual-fault POST/PUT/PATCH
paths.

Uses the same session-scoped `management_url` fixture and login pattern as
test_management_contract.py.
"""

import uuid

import httpx


def _login(base, username="admin", password="admin123"):
    r = httpx.post(f"{base}/api/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _unique_name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# providers.py :: create_provider
# regression: validation-order fix restores existing-name (409) ahead of the
# api_key-required (400) check -- see git show 44cc384 lines ~189-197.
# ---------------------------------------------------------------------------


def test_create_provider_name_conflict_beats_api_key_required(management_url):
    headers = _login(management_url)
    name = _unique_name("regression-provider")

    # Seed: create a provider with a valid api_key so the name exists.
    r = httpx.post(
        f"{management_url}/api/v1/providers",
        json={
            "name": name,
            "provider_type": "openai",
            "endpoint_url": "https://api.openai.com/v1",
            "api_key": "sk-seed-valid-key",
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text

    # Dual fault: name already exists AND api_key is missing (would also be a
    # 400 on its own). Original order checks name-conflict (409) first.
    r = httpx.post(
        f"{management_url}/api/v1/providers",
        json={"name": name, "provider_type": "openai", "endpoint_url": "https://api.openai.com/v1"},
        headers=headers,
    )
    assert r.status_code == 409, r.text
    assert r.json() == {"error": "Provider name already exists"}


# ---------------------------------------------------------------------------
# providers.py :: create_provider_credential
# regression: provider-exists (404) must run before label/api_key/weight
# validation; label-uniqueness (409) must run before weight-range (400).
# see git show 44cc384 lines ~471-509.
# ---------------------------------------------------------------------------


def test_create_provider_credential_provider_not_found_beats_weight_validation(management_url):
    headers = _login(management_url)

    # Dual fault: provider 999999 does not exist AND weight (99999) is out of
    # range. Original order checks provider existence (404) first.
    r = httpx.post(
        f"{management_url}/api/v1/providers/999999/credentials",
        json={"label": "x", "weight": 99999},
        headers=headers,
    )
    assert r.status_code == 404, r.text
    assert r.json() == {"status": "error", "error": "Provider not found"}


def test_create_provider_credential_label_conflict_beats_weight_validation(management_url):
    headers = _login(management_url)
    provider_name = _unique_name("regression-cred-provider")

    r = httpx.post(
        f"{management_url}/api/v1/providers",
        json={
            "name": provider_name,
            "provider_type": "openai",
            "endpoint_url": "https://api.openai.com/v1",
            "api_key": "sk-seed-valid-key",
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    provider_id = r.json()["id"]

    # Seed a credential with a known label.
    r = httpx.post(
        f"{management_url}/api/v1/providers/{provider_id}/credentials",
        json={"label": "dup-label", "api_key": "sk-cred-1"},
        headers=headers,
    )
    assert r.status_code == 201, r.text

    # Dual fault: label already exists for this provider AND weight (99999)
    # is out of range. Original order checks label-uniqueness (409) first.
    r = httpx.post(
        f"{management_url}/api/v1/providers/{provider_id}/credentials",
        json={"label": "dup-label", "api_key": "sk-cred-2", "weight": 99999},
        headers=headers,
    )
    assert r.status_code == 409, r.text
    assert r.json() == {
        "status": "error",
        "error": "Credential with label 'dup-label' already exists for this provider",
    }


# ---------------------------------------------------------------------------
# providers.py :: update_provider_credential
# regression: provider/credential existence (404) must run before the
# hoisted label/weight/api-key format checks. see git show 44cc384 lines
# ~546-561.
# ---------------------------------------------------------------------------


def test_update_provider_credential_provider_not_found_beats_weight_validation(management_url):
    headers = _login(management_url)

    # Dual fault: provider 999999 does not exist AND weight (99999) is out of
    # range. Original order checks provider existence (404) first.
    r = httpx.patch(
        f"{management_url}/api/v1/providers/999999/credentials/1",
        json={"weight": 99999},
        headers=headers,
    )
    assert r.status_code == 404, r.text
    assert r.json() == {"status": "error", "error": "Provider not found"}


# ---------------------------------------------------------------------------
# ollama.py :: pull_ollama_model
# regression: deployment-exists (404) is the first statement in the
# original, before get_json(). see git show 44cc384 lines ~339-349.
# ---------------------------------------------------------------------------


def test_pull_ollama_model_deployment_not_found_beats_body_validation(management_url):
    headers = _login(management_url)

    # Dual fault: deployment 999999 does not exist AND no body is sent
    # (would also be a 400 "model is required" on its own). Original order
    # checks deployment existence (404) first.
    r = httpx.post(
        f"{management_url}/api/v1/ollama/deployments/999999/models/pull",
        headers=headers,
    )
    assert r.status_code == 404, r.text
    assert r.json() == {"error": "Deployment not found"}


# ---------------------------------------------------------------------------
# llamacpp.py :: update_llamacpp_deployment
# regression: deployment-exists (404) must run before request.get_json() is
# parsed, so a non-JSON/absent content-type does not trip Quart's default
# 400 ahead of the existence check. see git show 44cc384 lines ~102-108.
# ---------------------------------------------------------------------------


def test_update_llamacpp_deployment_not_found_beats_body_parse(management_url):
    headers = _login(management_url)

    # Dual fault: deployment 999999 does not exist AND the request body is
    # non-JSON with no JSON content-type (would trip Quart's default 400 on
    # get_json() if parsed first). Original order checks deployment
    # existence (404) first.
    r = httpx.patch(
        f"{management_url}/api/v1/llamacpp/deployments/999999",
        content=b"not-json-content",
        headers={**headers, "Content-Type": "text/plain"},
    )
    assert r.status_code == 404, r.text
    assert r.json() == {"error": "Deployment not found"}
