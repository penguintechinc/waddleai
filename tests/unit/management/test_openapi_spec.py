"""Unit tests for the OpenAPI documentation endpoints.

Guards the split required by backend.md OpenAPI: quart-schema's default,
fully-unauthenticated mount points must be disabled, the public document
must expose exactly the login path, and the full spec + Swagger UI must
require the same authentication as the rest of the API.
"""


class TestDefaultQuartSchemaMountDisabled:
    """quart-schema's own auto-mounted routes must never be reachable."""

    async def test_default_openapi_json_not_mounted(self, client) -> None:
        """The default /openapi.json route must not exist (404, not 401)."""
        resp = await client.get("/openapi.json")
        assert resp.status_code == 404

    async def test_default_swagger_docs_not_mounted(self, client) -> None:
        """The default /docs Swagger UI route must not exist."""
        resp = await client.get("/docs")
        assert resp.status_code == 404

    async def test_default_redoc_not_mounted(self, client) -> None:
        """The default /redocs Redoc UI route must not exist."""
        resp = await client.get("/redocs")
        assert resp.status_code == 404

    async def test_default_scalar_not_mounted(self, client) -> None:
        """The default /scalar UI route must not exist."""
        resp = await client.get("/scalar")
        assert resp.status_code == 404


class TestPublicOpenApiSpec:
    """GET /api/v1/openapi/public.json -- unauthenticated, login-only."""

    async def test_public_spec_accessible_without_auth(self, client) -> None:
        """The public document is reachable with no Authorization header."""
        resp = await client.get("/api/v1/openapi/public.json")
        assert resp.status_code == 200

    async def test_public_spec_contains_only_login_path(self, client) -> None:
        """The public document exposes exactly one path: login."""
        resp = await client.get("/api/v1/openapi/public.json")
        spec = await resp.get_json()
        assert list(spec["paths"].keys()) == ["/api/v1/auth/login"]

    async def test_public_spec_login_path_has_post_operation(self, client) -> None:
        """The login path documents its real POST operation."""
        resp = await client.get("/api/v1/openapi/public.json")
        spec = await resp.get_json()
        assert "post" in spec["paths"]["/api/v1/auth/login"]

    async def test_public_spec_is_valid_openapi_document(self, client) -> None:
        """The public document is a well-formed OpenAPI 3.x document on its own."""
        resp = await client.get("/api/v1/openapi/public.json")
        spec = await resp.get_json()
        assert spec["openapi"].startswith("3.")
        assert "info" in spec
        assert "components" in spec


class TestFullOpenApiSpecRequiresAuth:
    """GET /api/v1/openapi/full.json -- gated behind require_auth like the rest of the API."""

    async def test_full_spec_rejected_without_auth(self, client) -> None:
        """No Authorization header -> 401, same as any other protected endpoint."""
        resp = await client.get("/api/v1/openapi/full.json")
        assert resp.status_code == 401

    async def test_full_spec_rejected_with_invalid_token(self, client) -> None:
        """A malformed bearer token -> 401."""
        resp = await client.get(
            "/api/v1/openapi/full.json",
            headers={"Authorization": "Bearer bad.token.here"},
        )
        assert resp.status_code == 401

    async def test_full_spec_accessible_with_valid_auth(self, client, auth_headers: dict) -> None:
        """A valid bearer token reaches the full document."""
        resp = await client.get("/api/v1/openapi/full.json", headers=auth_headers)
        assert resp.status_code == 200

    async def test_full_spec_covers_far_more_than_login(self, client, auth_headers: dict) -> None:
        """The full spec must include far more than the one public path."""
        resp = await client.get("/api/v1/openapi/full.json", headers=auth_headers)
        spec = await resp.get_json()
        assert len(spec["paths"]) > 50
        assert "/api/v1/auth/login" in spec["paths"]
        assert "/api/v1/keys" in spec["paths"]


class TestSwaggerDocsUiRequiresAuth:
    """GET /api/v1/docs -- Swagger UI, gated the same way as the full spec."""

    async def test_docs_ui_rejected_without_auth(self, client) -> None:
        """No Authorization header -> 401, not a rendered (and leaking) UI page."""
        resp = await client.get("/api/v1/docs")
        assert resp.status_code == 401

    async def test_docs_ui_accessible_with_valid_auth(self, client, auth_headers: dict) -> None:
        """A valid bearer token renders the Swagger UI, pointed at the gated full spec."""
        resp = await client.get("/api/v1/docs", headers=auth_headers)
        assert resp.status_code == 200
        body = await resp.get_data(as_text=True)
        assert "swagger-ui" in body
        assert "/api/v1/openapi/full.json" in body
