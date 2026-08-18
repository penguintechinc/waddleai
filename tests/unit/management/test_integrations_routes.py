"""Unit tests for the §11.4 external-MCP gateway management routes.

`/api/v1/integrations/mcp-endpoints` CRUD (org-scoped, admin only, secrets
encrypted/never echoed), `/api/v1/integrations/opencode-config`
(self-service), and the per-user OAuth2 link initiate/callback.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from passlib.hash import bcrypt

from tests.unit.management.conftest import make_select_result

ENDPOINT_PATH = "/api/v1/integrations/mcp-endpoints"

# Named constants (rather than inline literals) so bandit's hardcoded-
# password heuristic -- which fires on any `client_secret=`/`access_token=`/
# `*token*=` keyword argument or variable name holding a string literal --
# doesn't flag these fixture-only values.
MOCK_UPSTREAM_ACCESS_TOKEN = "upstream-access-token"  # noqa: S105 -- test fixture value
MOCK_UPSTREAM_REFRESH_TOKEN = "upstream-refresh-token"  # noqa: S105 -- test fixture value


def _enable_flag(monkeypatch) -> None:
    """Turn `waddleai.mcp_v2` on for the duration of one test."""
    monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "1")


def make_mock_endpoint(
    endpoint_id: int = 1,
    org_id: int = 1,
    name: str = "Elder",
    url: str = "https://elder.example.com/mcp",
    transport: str = "streamable_http",
    auth_type: str = "header",
    auth_config: dict | None = None,
    identity_mode: str = "shared",
    namespace: str = "elder",
    credentials_ref: str | None = None,
    status: str = "active",
) -> MagicMock:
    """Return a MagicMock representing an `mcp_endpoints` row."""
    row = MagicMock()
    row.id = endpoint_id
    row.org_id = org_id
    row.name = name
    row.url = url
    row.transport = transport
    row.auth_type = auth_type
    row.auth_config = (
        auth_config if auth_config is not None else {"header_value": "Bearer secret-token"}
    )
    row.identity_mode = identity_mode
    row.namespace = namespace
    row.credentials_ref = credentials_ref
    row.status = status
    row.created_at = datetime(2026, 1, 1, 12, 0, 0)
    return row


def make_mock_virtual_key(
    key_id: int = 1, user_id: int = 1, org_id: int = 1, raw_key: str = "wa-testkey123"
) -> MagicMock:
    """Return a MagicMock `virtual_keys` row whose `key_hash` matches `raw_key`."""
    key = MagicMock()
    key.id = key_id
    key.user_id = user_id
    key.organization_id = org_id
    key.enabled = True
    key.key_hash = bcrypt.hash(raw_key)
    return key


class TestListMcpEndpoints:
    """GET /api/v1/integrations/mcp-endpoints."""

    async def test_requires_admin(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict, monkeypatch
    ) -> None:
        """A non-admin caller is rejected with 403."""
        _enable_flag(monkeypatch)
        resp = await client.get(ENDPOINT_PATH, headers=user_auth_headers)
        assert resp.status_code == 403

    async def test_requires_auth(self, client) -> None:
        """An unauthenticated caller is rejected with 401."""
        resp = await client.get(ENDPOINT_PATH)
        assert resp.status_code == 401

    async def test_flag_off_returns_404(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """With `waddleai.mcp_v2` off, the endpoint is inert (404)."""
        monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "0")
        resp = await client.get(ENDPOINT_PATH, headers=auth_headers)
        assert resp.status_code == 404

    async def test_lists_org_scoped_endpoints(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Listing returns the org's endpoints with secrets masked, never plaintext."""
        _enable_flag(monkeypatch)
        endpoint = make_mock_endpoint()
        app_mock_db.return_value.select.return_value = make_select_result([endpoint])

        resp = await client.get(ENDPOINT_PATH, headers=auth_headers)
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["status"] == "success"
        assert len(body["data"]) == 1
        assert body["data"][0]["namespace"] == "elder"
        # Secret auth_config field is masked, never plaintext.
        assert body["data"][0]["auth_config"]["header_value"] != "Bearer secret-token"
        assert "****" in body["data"][0]["auth_config"]["header_value"]


class TestCreateMcpEndpoint:
    """POST /api/v1/integrations/mcp-endpoints."""

    def _payload(self, **overrides) -> dict:
        payload = {
            "name": "Elder",
            "url": "https://elder.example.com/mcp",
            "transport": "streamable_http",
            "auth_type": "header",
            "auth_config": {"header_value": "Bearer plaintext-secret"},
            "identity_mode": "shared",
            "namespace": "elder",
        }
        payload.update(overrides)
        return payload

    async def test_requires_admin(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict, monkeypatch
    ) -> None:
        """A non-admin caller is rejected with 403."""
        _enable_flag(monkeypatch)
        resp = await client.post(ENDPOINT_PATH, headers=user_auth_headers, json=self._payload())
        assert resp.status_code == 403

    async def test_creates_and_encrypts_secret(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A created endpoint's secret auth_config field is encrypted before insert."""
        _enable_flag(monkeypatch)
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "test-encryption-key")

        empty_sel = make_select_result([])
        created_row = make_mock_endpoint(auth_config={"header_value": "enc:whatever"})
        created_sel = make_select_result([created_row])
        app_mock_db.return_value.select.side_effect = [empty_sel, created_sel]
        app_mock_db.mcp_endpoints.insert.return_value = 1

        resp = await client.post(ENDPOINT_PATH, headers=auth_headers, json=self._payload())
        assert resp.status_code == 201
        body = await resp.get_json()
        assert body["status"] == "success"
        assert body["data"]["namespace"] == "elder"

        insert_kwargs = app_mock_db.mcp_endpoints.insert.call_args.kwargs
        assert insert_kwargs["org_id"] == 1
        stored_auth_config = insert_kwargs["auth_config"]
        assert stored_auth_config["header_value"].startswith("enc:")
        assert stored_auth_config["header_value"] != "Bearer plaintext-secret"

    async def test_namespace_conflict_returns_409(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Registering a namespace already used by this org returns 409."""
        _enable_flag(monkeypatch)
        existing = make_mock_endpoint()
        app_mock_db.return_value.select.return_value = make_select_result([existing])

        resp = await client.post(ENDPOINT_PATH, headers=auth_headers, json=self._payload())
        assert resp.status_code == 409

    async def test_rejects_invalid_transport(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """An unsupported transport value is rejected with 400."""
        _enable_flag(monkeypatch)
        resp = await client.post(
            ENDPOINT_PATH, headers=auth_headers, json=self._payload(transport="carrier-pigeon")
        )
        assert resp.status_code == 400

    async def test_ignores_client_supplied_org_id(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """org_id always comes from the caller's token, never the request body."""
        _enable_flag(monkeypatch)
        empty_sel = make_select_result([])
        created_row = make_mock_endpoint()
        created_sel = make_select_result([created_row])
        app_mock_db.return_value.select.side_effect = [empty_sel, created_sel]
        app_mock_db.mcp_endpoints.insert.return_value = 1

        resp = await client.post(
            ENDPOINT_PATH, headers=auth_headers, json=self._payload(org_id=999)
        )
        assert resp.status_code == 201
        insert_kwargs = app_mock_db.mcp_endpoints.insert.call_args.kwargs
        assert insert_kwargs["org_id"] == 1  # from auth_headers' admin token, org 1 -- not 999


class TestGetUpdateDeleteMcpEndpoint:
    """GET/PUT/DELETE /api/v1/integrations/mcp-endpoints/<id>."""

    async def test_get_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A non-existent endpoint id returns 404."""
        _enable_flag(monkeypatch)
        app_mock_db.return_value.select.return_value = make_select_result([])
        resp = await client.get(f"{ENDPOINT_PATH}/999", headers=auth_headers)
        assert resp.status_code == 404

    async def test_get_foreign_org_returns_403(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """An endpoint belonging to another org returns 403, not 404."""
        _enable_flag(monkeypatch)
        foreign = make_mock_endpoint(org_id=99)
        app_mock_db.return_value.select.return_value = make_select_result([foreign])
        resp = await client.get(f"{ENDPOINT_PATH}/1", headers=auth_headers)
        assert resp.status_code == 403

    async def test_get_own_org_succeeds(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """An endpoint belonging to the caller's own org is fetched successfully."""
        _enable_flag(monkeypatch)
        endpoint = make_mock_endpoint(org_id=1)
        app_mock_db.return_value.select.return_value = make_select_result([endpoint])
        resp = await client.get(f"{ENDPOINT_PATH}/1", headers=auth_headers)
        assert resp.status_code == 200

    async def test_update_own_org_succeeds(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Updating a field on the caller's own endpoint persists and is reflected back."""
        _enable_flag(monkeypatch)
        endpoint = make_mock_endpoint(org_id=1)
        updated = make_mock_endpoint(org_id=1, name="Elder v2")
        app_mock_db.return_value.select.side_effect = [
            make_select_result([endpoint]),
            make_select_result([updated]),
        ]
        resp = await client.put(
            f"{ENDPOINT_PATH}/1", headers=auth_headers, json={"name": "Elder v2"}
        )
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["data"]["name"] == "Elder v2"

    async def test_update_foreign_org_returns_403(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Updating another org's endpoint returns 403."""
        _enable_flag(monkeypatch)
        foreign = make_mock_endpoint(org_id=99)
        app_mock_db.return_value.select.return_value = make_select_result([foreign])
        resp = await client.put(
            f"{ENDPOINT_PATH}/1", headers=auth_headers, json={"name": "Hijacked"}
        )
        assert resp.status_code == 403

    async def test_delete_own_org_succeeds(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Deleting the caller's own endpoint succeeds."""
        _enable_flag(monkeypatch)
        endpoint = make_mock_endpoint(org_id=1)
        app_mock_db.return_value.select.return_value = make_select_result([endpoint])
        resp = await client.delete(f"{ENDPOINT_PATH}/1", headers=auth_headers)
        assert resp.status_code == 200

    async def test_delete_foreign_org_returns_403(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Deleting another org's endpoint returns 403."""
        _enable_flag(monkeypatch)
        foreign = make_mock_endpoint(org_id=99)
        app_mock_db.return_value.select.return_value = make_select_result([foreign])
        resp = await client.delete(f"{ENDPOINT_PATH}/1", headers=auth_headers)
        assert resp.status_code == 403


class TestOpencodeConfig:
    """POST /api/v1/integrations/opencode-config.

    POST-with-body, not GET-with-query-param: a `wa-` key is a bearer
    credential, and a query string is written to ingress/access logs, any
    CDN/proxy in front, browser history, and outbound Referer headers.
    """

    async def test_requires_auth(self, client) -> None:
        """An unauthenticated caller is rejected with 401."""
        resp = await client.post(
            "/api/v1/integrations/opencode-config", json={"virtual_key": "wa-x"}
        )
        assert resp.status_code == 401

    async def test_get_is_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Regression: GET (which would put the key in a logged query string) is rejected."""
        _enable_flag(monkeypatch)
        resp = await client.get(
            "/api/v1/integrations/opencode-config?virtual_key=wa-x", headers=auth_headers
        )
        assert resp.status_code == 405

    async def test_missing_virtual_key_is_400(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Omitting virtual_key from the request body is rejected with 400."""
        _enable_flag(monkeypatch)
        resp = await client.post(
            "/api/v1/integrations/opencode-config", headers=auth_headers, json={}
        )
        assert resp.status_code == 400

    async def test_unrecognized_key_is_403(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A virtual_key the caller doesn't own is rejected with 403."""
        _enable_flag(monkeypatch)
        app_mock_db.return_value.select.return_value = make_select_result([])
        resp = await client.post(
            "/api/v1/integrations/opencode-config",
            headers=auth_headers,
            json={"virtual_key": "wa-unknown"},
        )
        assert resp.status_code == 403

    async def test_renders_config_for_owned_key(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A caller's own virtual_key renders a config with the proxy URL and that key."""
        _enable_flag(monkeypatch)
        raw_key = "wa-mykeyvalue"
        key_row = make_mock_virtual_key(user_id=1, org_id=1, raw_key=raw_key)
        app_mock_db.return_value.select.return_value = make_select_result([key_row])

        resp = await client.post(
            "/api/v1/integrations/opencode-config",
            headers=auth_headers,
            json={"virtual_key": raw_key},
        )
        assert resp.status_code == 200
        body = await resp.get_json()
        config = body["data"]
        assert config["provider"]["waddleai"]["apiKey"] == raw_key
        assert config["mcp"]["waddleai"]["url"].endswith("/mcp")
        assert config["provider"]["waddleai"]["models"].endswith("/v1/models")


class TestLinkFlow:
    """GET /api/v1/integrations/mcp-endpoints/<id>/link[/callback]."""

    async def test_link_requires_per_user_endpoint(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Linking a `shared`-identity endpoint is rejected with 400."""
        _enable_flag(monkeypatch)
        endpoint = make_mock_endpoint(org_id=1, identity_mode="shared")
        app_mock_db.return_value.select.return_value = make_select_result([endpoint])
        resp = await client.get(f"{ENDPOINT_PATH}/1/link", headers=auth_headers)
        assert resp.status_code == 400

    async def test_link_with_preregistered_client_returns_auth_url(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A per_user endpoint with a pre-registered client_id returns an authorization URL."""
        _enable_flag(monkeypatch)
        endpoint = make_mock_endpoint(
            org_id=1,
            identity_mode="per_user",
            auth_type="oauth2_auth_code",
            auth_config={
                "authorization_endpoint": "https://elder.example.com/authorize",
                "token_endpoint": "https://elder.example.com/token",
                "client_id": "preregistered-client",
                "client_secret": "shh",
            },
        )
        app_mock_db.return_value.select.return_value = make_select_result([endpoint])

        resp = await client.get(f"{ENDPOINT_PATH}/1/link", headers=auth_headers)
        assert resp.status_code == 200
        body = await resp.get_json()
        auth_url = body["data"]["authorization_url"]
        assert auth_url.startswith("https://elder.example.com/authorize")
        assert "client_id=preregistered-client" in auth_url

    async def test_link_foreign_org_returns_403(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Linking another org's endpoint returns 403."""
        _enable_flag(monkeypatch)
        foreign = make_mock_endpoint(
            org_id=99, identity_mode="per_user", auth_type="oauth2_auth_code"
        )
        app_mock_db.return_value.select.return_value = make_select_result([foreign])
        resp = await client.get(f"{ENDPOINT_PATH}/1/link", headers=auth_headers)
        assert resp.status_code == 403

    async def test_callback_rejects_invalid_state(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A tampered/unsigned state parameter is rejected with 400."""
        _enable_flag(monkeypatch)
        resp = await client.get(
            f"{ENDPOINT_PATH}/1/link/callback?code=abc&state=tampered", headers=auth_headers
        )
        assert resp.status_code == 400

    async def test_callback_stores_link_and_never_echoes_token(
        self, client, flask_app, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A valid callback stores the encrypted token and never returns it in the response."""
        _enable_flag(monkeypatch)
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "test-encryption-key")
        from services.management.app.api.v1.integrations import _sign_link_state

        endpoint = make_mock_endpoint(
            org_id=1,
            identity_mode="per_user",
            auth_type="oauth2_auth_code",
            auth_config={
                "authorization_endpoint": "https://elder.example.com/authorize",
                "token_endpoint": "https://elder.example.com/token",
                "client_id": "preregistered-client",
                "client_secret": "shh",
            },
        )
        app_mock_db.return_value.select.side_effect = [
            make_select_result([endpoint]),  # _get_org_scoped_endpoint
            make_select_result([]),  # _upsert_link: no existing link -> insert
        ]
        app_mock_db.mcp_user_links.insert.return_value = 1

        # We don't have a real OAuth server wired to this route test, so
        # exercise the state-verification/DB-write path with a mocked
        # OutboundAuth.exchange_code.
        from shared.mcp.gateway.auth import TokenSet

        async with flask_app.app_context():
            state = _sign_link_state(1, 1)
        fake_token = TokenSet(
            access_token=MOCK_UPSTREAM_ACCESS_TOKEN,
            refresh_token=MOCK_UPSTREAM_REFRESH_TOKEN,
            expires_at=None,
        )
        with patch(
            "services.management.app.api.v1.integrations.OutboundAuth.exchange_code",
            new=AsyncMock(return_value=fake_token),
        ):
            resp = await client.get(
                f"{ENDPOINT_PATH}/1/link/callback?code=authcode&state={state}", headers=auth_headers
            )

        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["data"]["linked"] is True
        raw_body = await resp.get_data(as_text=True)
        assert MOCK_UPSTREAM_ACCESS_TOKEN not in raw_body
        assert MOCK_UPSTREAM_REFRESH_TOKEN not in raw_body

        insert_kwargs = app_mock_db.mcp_user_links.insert.call_args.kwargs
        assert insert_kwargs["access_token_enc"] != MOCK_UPSTREAM_ACCESS_TOKEN
        assert insert_kwargs["access_token_enc"].startswith("enc:")
        assert insert_kwargs["user_uuid"] == "1"
