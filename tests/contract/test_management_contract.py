"""Golden-snapshot contract tests for the management service's `/api/v1/*` HTTP surface.

Captured against the current (Flask/WSGI) implementation. These snapshots
are the behavior baseline the upcoming Flask -> Quart migration must
preserve.

Do not modify `conftest.py` or `snapshot.py` -- see tests/contract/ for the
shared harness (`management_url` fixture, `assert_snapshot`).
"""

import httpx

from tests.contract.snapshot import assert_snapshot


def _login(base, username="admin", password="admin123"):  # noqa: S107 -- seeded contract-test admin credential, not a production default
    r = httpx.post(f"{base}/api/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _create_user(base, admin_headers, username, password, role="user"):
    r = httpx.post(
        f"{base}/api/v1/users",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": password,
            "role": role,
        },
        headers=admin_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _drop_keys(obj, *keys):
    """Recursively strip keys that snapshot.py's normalizer does not catch.

    `last_login_at` is an ISO timestamp that is None on a user's first-ever
    login but becomes a real (previous-login) wall-clock value once that
    user has logged in more than once within a test session. Since every
    contract test authenticates via `_login`, by the time later tests run,
    admin has already logged in multiple times -- so this field holds a
    real timestamp that differs between the CONTRACT_RECORD run and any
    later replay run. snapshot.py's `_VOLATILE_KEYS` does not include
    `last_login_at` (only `id`/`created_at`/etc.), so it is excluded here
    instead of editing the shared normalizer.
    """
    if isinstance(obj, dict):
        return {k: _drop_keys(v, *keys) for k, v in obj.items() if k not in keys}
    if isinstance(obj, list):
        return [_drop_keys(v, *keys) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def test_auth_login_shape(management_url):
    """Pin the /auth/login response shape for valid admin credentials."""
    r = httpx.post(
        f"{management_url}/api/v1/auth/login",
        json={"username": "admin", "password": "admin123"},
    )
    body = r.json()
    # access_token is dynamically generated; pop it and verify presence separately
    access_token = body.pop("access_token")
    assert isinstance(access_token, str) and len(access_token) > 0
    assert_snapshot("mgmt_auth_login", status=r.status_code, body=body)


def test_auth_login_bad_creds(management_url):
    """Pin the /auth/login response shape for a wrong password."""
    r = httpx.post(
        f"{management_url}/api/v1/auth/login",
        json={"username": "admin", "password": "wrong"},
    )
    assert_snapshot("mgmt_auth_login_bad", status=r.status_code, body=r.json())


def test_error_400_missing_field(management_url):
    """Pin the 400 error body shape for /auth/login when password is missing."""
    # Test 400 error format: POST /auth/login with missing required field
    r = httpx.post(
        f"{management_url}/api/v1/auth/login",
        json={"username": "admin"},  # missing password
    )
    assert_snapshot("mgmt_error_400", status=r.status_code, body=r.json())


def test_auth_me(management_url):
    """Pin the /auth/me response shape for an authenticated admin request."""
    r = httpx.get(f"{management_url}/api/v1/auth/me", headers=_login(management_url))
    assert_snapshot(
        "mgmt_auth_me", status=r.status_code, body=_drop_keys(r.json(), "last_login_at")
    )


def test_auth_verify(management_url):
    """Pin the /auth/verify response shape for an authenticated admin request."""
    r = httpx.get(f"{management_url}/api/v1/auth/verify", headers=_login(management_url))
    assert_snapshot("mgmt_auth_verify", status=r.status_code, body=r.json())


def test_auth_refresh(management_url):
    """Pin the /auth/refresh response shape (token popped before snapshotting)."""
    r = httpx.post(f"{management_url}/api/v1/auth/refresh", headers=_login(management_url))
    body = r.json()
    # access_token is dynamically generated; pop it to match login test pattern
    access_token = body.pop("access_token")
    assert isinstance(access_token, str) and len(access_token) > 0
    assert_snapshot("mgmt_auth_refresh", status=r.status_code, body=body)


def test_auth_logout(management_url):
    """Pin the /auth/logout response shape."""
    r = httpx.post(f"{management_url}/api/v1/auth/logout", headers=_login(management_url))
    assert_snapshot("mgmt_auth_logout", status=r.status_code, body=r.json())


# ---------------------------------------------------------------------------
# organizations
# ---------------------------------------------------------------------------


def test_orgs_list_requires_auth(management_url):
    """Pin the unauthenticated response shape for GET /organizations."""
    r = httpx.get(f"{management_url}/api/v1/organizations")
    assert_snapshot("mgmt_orgs_unauth", status=r.status_code, body=r.json())


def test_orgs_list(management_url):
    """Pin the authenticated GET /organizations response shape."""
    r = httpx.get(f"{management_url}/api/v1/organizations", headers=_login(management_url))
    assert_snapshot("mgmt_orgs_list", status=r.status_code, body=r.json())


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


def test_users_list(management_url):
    """Pin the GET /users response shape (last_login_at dropped, mutates across runs)."""
    r = httpx.get(f"{management_url}/api/v1/users", headers=_login(management_url))
    assert_snapshot(
        "mgmt_users_list", status=r.status_code, body=_drop_keys(r.json(), "last_login_at")
    )


# ---------------------------------------------------------------------------
# keys
# ---------------------------------------------------------------------------


def test_keys_list(management_url):
    """Pin the GET /keys response shape."""
    r = httpx.get(f"{management_url}/api/v1/keys", headers=_login(management_url))
    assert_snapshot("mgmt_keys_list", status=r.status_code, body=r.json())


# ---------------------------------------------------------------------------
# quotas
# ---------------------------------------------------------------------------


def test_quotas_list(management_url):
    """Pin the GET /quotas response shape."""
    r = httpx.get(f"{management_url}/api/v1/quotas", headers=_login(management_url))
    assert_snapshot("mgmt_quotas_list", status=r.status_code, body=r.json())


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------


def test_usage_summary(management_url):
    """Pin the GET /usage/summary response shape (date/month dropped, change daily)."""
    r = httpx.get(f"{management_url}/api/v1/usage/summary", headers=_login(management_url))
    # Drop date/month fields (date.today()-derived, changes every day)
    assert_snapshot(
        "mgmt_usage_summary", status=r.status_code, body=_drop_keys(r.json(), "date", "month")
    )


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------


def test_providers_types(management_url):
    """Pin the GET /providers/types response shape."""
    r = httpx.get(f"{management_url}/api/v1/providers/types", headers=_login(management_url))
    assert_snapshot("mgmt_providers_types", status=r.status_code, body=r.json())


# ---------------------------------------------------------------------------
# ollama + ollama_models
# ---------------------------------------------------------------------------


def test_ollama_deployments_list(management_url):
    """Pin the GET /ollama/deployments response shape."""
    r = httpx.get(f"{management_url}/api/v1/ollama/deployments", headers=_login(management_url))
    assert_snapshot("mgmt_ollama_deployments_list", status=r.status_code, body=r.json())


def test_ollama_models_list(management_url):
    """Pin the GET /ollama/models response shape."""
    r = httpx.get(f"{management_url}/api/v1/ollama/models", headers=_login(management_url))
    assert_snapshot("mgmt_ollama_models_list", status=r.status_code, body=r.json())


# ---------------------------------------------------------------------------
# llamacpp
# ---------------------------------------------------------------------------


def test_llamacpp_deployments_list(management_url):
    """Pin the GET /llamacpp/deployments response shape."""
    r = httpx.get(f"{management_url}/api/v1/llamacpp/deployments", headers=_login(management_url))
    assert_snapshot("mgmt_llamacpp_deployments_list", status=r.status_code, body=r.json())


# ---------------------------------------------------------------------------
# memory-config / rag-config / embedding-config
#
# Re-homed from the deleted MarchProxy AILB coupling (formerly under the
# `/ailb/*` prefix) to their own top-level paths -- see
# services/management/app/api/v1/memory_config.py.
# ---------------------------------------------------------------------------


def test_memory_config(management_url):
    """Pin the GET /memory-config response shape for organization_id=1."""
    r = httpx.get(
        f"{management_url}/api/v1/memory-config",
        params={"organization_id": 1},
        headers=_login(management_url),
    )
    assert_snapshot("mgmt_memory_config", status=r.status_code, body=r.json())


def test_memory_config_rag(management_url):
    """Pin the GET /rag-config response shape for organization_id=1."""
    # Ported legacy memory-config admin surface parity (task C1): RAG
    # injection config GET, same organization-scoped shape as memory-config.
    r = httpx.get(
        f"{management_url}/api/v1/rag-config",
        params={"organization_id": 1},
        headers=_login(management_url),
    )
    assert_snapshot("mgmt_memory_config_rag", status=r.status_code, body=r.json())


def test_memory_config_embedding(management_url):
    """Pin the GET /embedding-config response shape (global default, no organization_id)."""
    # Ported legacy memory-config admin surface parity (task C1): embedding
    # backend config GET, global default (no organization_id).
    r = httpx.get(f"{management_url}/api/v1/embedding-config", headers=_login(management_url))
    assert_snapshot("mgmt_memory_config_embedding", status=r.status_code, body=r.json())


# ---------------------------------------------------------------------------
# routing_assignments / routing_policies / routing_rules / model_aliases /
# routing_decisions (spec §7.6) -- `routing_matrix` (and its Redis-backed
# `/instructions` NL-routing surface, superseded by routing_policies'
# classifier_prompt) was retired and renamed to routing_assignments.
# ---------------------------------------------------------------------------


def test_routing_assignments_list(management_url):
    """Pin the GET /routing/assignments/ response shape."""
    r = httpx.get(f"{management_url}/api/v1/routing/assignments/", headers=_login(management_url))
    assert_snapshot("mgmt_routing_assignments_list", status=r.status_code, body=r.json())


def test_routing_policies_get_defaults(management_url):
    """Pin the GET /routing/policies/1 response on the engine-defaults fallback path."""
    # Org 1 (the seeded admin's org) has no routing_policies row yet -- the
    # engine-defaults fallback path.
    r = httpx.get(f"{management_url}/api/v1/routing/policies/1", headers=_login(management_url))
    assert_snapshot("mgmt_routing_policies_get_defaults", status=r.status_code, body=r.json())


def test_routing_rules_list(management_url):
    """Pin the GET /routing/rules/ response shape."""
    r = httpx.get(f"{management_url}/api/v1/routing/rules/", headers=_login(management_url))
    assert_snapshot("mgmt_routing_rules_list", status=r.status_code, body=r.json())


def test_model_aliases_list(management_url):
    """Pin the GET /routing/aliases/ response shape."""
    r = httpx.get(f"{management_url}/api/v1/routing/aliases/", headers=_login(management_url))
    assert_snapshot("mgmt_model_aliases_list", status=r.status_code, body=r.json())


def test_routing_decisions_summary(management_url):
    """Pin the GET /routing/decisions/ response shape."""
    r = httpx.get(f"{management_url}/api/v1/routing/decisions/", headers=_login(management_url))
    assert_snapshot("mgmt_routing_decisions_summary", status=r.status_code, body=r.json())


# ---------------------------------------------------------------------------
# error formats
# ---------------------------------------------------------------------------


def test_error_404_unknown_route(management_url):
    """Pin the 404 error body shape for an unknown /api/v1 route."""
    r = httpx.get(f"{management_url}/api/v1/this-route-does-not-exist")
    assert_snapshot("mgmt_error_404", status=r.status_code, body=r.json())


# ---------------------------------------------------------------------------
# auth behavior: 403 wrong role (must run after the shape snapshots above --
# it mutates state by creating a second user)
# ---------------------------------------------------------------------------


def test_forbidden_wrong_role(management_url):
    """Pin the 403 response shape when a non-admin user attempts to create an org."""
    admin_headers = _login(management_url)
    _create_user(management_url, admin_headers, "contract_role_user", "RoleUserPass1", role="user")
    user_headers = _login(management_url, "contract_role_user", "RoleUserPass1")

    r = httpx.post(
        f"{management_url}/api/v1/organizations",
        json={"name": "should-not-be-created"},
        headers=user_headers,
    )
    assert_snapshot("mgmt_orgs_create_forbidden", status=r.status_code, body=r.json())


def test_auth_change_password(management_url):
    """Pin the /auth/change-password response shape for a successful password change."""
    admin_headers = _login(management_url)
    _create_user(management_url, admin_headers, "contract_pw_user", "OrigPass123", role="user")
    user_headers = _login(management_url, "contract_pw_user", "OrigPass123")

    r = httpx.post(
        f"{management_url}/api/v1/auth/change-password",
        json={"current_password": "OrigPass123", "new_password": "NewPass1234"},
        headers=user_headers,
    )
    assert_snapshot("mgmt_auth_change_password", status=r.status_code, body=r.json())
