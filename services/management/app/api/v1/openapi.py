"""WaddleAI Management API v1 - OpenAPI documentation endpoints.

Wires quart-schema into the app with every default (unauthenticated) mount
point disabled, then republishes exactly two documents per backend.md
OpenAPI: a minimal public spec containing only the login path (the one
documented exception to "docs/spec endpoints MUST be authenticated") and a
full spec + Swagger UI gated behind the same `require_auth` middleware as
the rest of the API. A live spec is the first thing an attacker enumerates,
so quart-schema's default `/openapi.json` / `/docs` / `/redocs` / `/scalar`
routes (fully unauthenticated, cover every endpoint) are never mounted.
"""

from __future__ import annotations

from typing import Any

from quart import Blueprint, Quart, current_app, jsonify
from quart_schema import Info, QuartSchema

# quart_schema 0.19.x has no public "build the full schema dict" API -- the
# click `schema` CLI command and the (disabled) default /openapi.json route
# both call this private helper directly. requirements.in exact-pins
# quart-schema==0.19.0 specifically so this import can't silently break on
# a minor bump.
from quart_schema.extension import _build_openapi_schema

from .auth import require_auth

LOGIN_PATH = "/api/v1/auth/login"

openapi_bp = Blueprint("openapi_docs", __name__)

# Subresource Integrity hashes pinned to the exact cdnjs asset version below
# (sourced from https://api.cdnjs.com/libraries/swagger-ui/5.4.2?fields=sri,
# cross-checked against a local sha512 of the fetched files) -- a compromised
# CDN response is rejected by the browser rather than executed.
_SWAGGER_CSS_SRI = (
    "sha512-wjyFPe3jl9Y/d+vaEDd04b2+wzgLdgKPVoy9m1FYNpJSMHM328"
    "G50WPU57xayVkZwxWi45vA+4QN+9erPZIeig=="
)
_SWAGGER_JS_SRI = (
    "sha512-l5/LYUvqCJKr9NblefbJPkYEpD3yUiOOXiLUKlibcIbVynsY3Uo"
    "pPYBr0xisoV8By8MulIth7yTLc+KOVt6YeQ=="
)

_SWAGGER_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
  <title>WaddleAI Management API - Docs</title>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/5.4.2/swagger-ui.min.css"
        integrity="{css_sri}" crossorigin="anonymous">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/5.4.2/swagger-ui-bundle.js"
          integrity="{js_sri}" crossorigin="anonymous"></script>
  <script>
    SwaggerUIBundle({{
      url: "{spec_url}",
      dom_id: "#swagger-ui",
      requestInterceptor: (req) => {{
        const token = window.sessionStorage.getItem("waddleai_docs_token");
        if (token) {{ req.headers["Authorization"] = "Bearer " + token; }}
        return req;
      }},
    }});
  </script>
</body>
</html>
"""


def init_openapi(app: Quart) -> QuartSchema:
    """Attach quart-schema to *app* with all default (unauthenticated) routes disabled."""
    return QuartSchema(
        app,
        openapi_path=None,
        redoc_ui_path=None,
        scalar_ui_path=None,
        swagger_ui_path=None,
        info=Info(title="WaddleAI Management API", version="1"),
        security_schemes={
            "bearerAuth": {"type": "http", "scheme": "bearer", "bearer_format": "JWT"},
        },
    )


def build_full_schema(app: Quart) -> dict[str, Any]:
    """Build the complete OpenAPI document for every registered route."""
    ext = app.extensions["QUART_SCHEMA"]
    schema = _build_openapi_schema(app, ext)
    _fix_security_scheme_casing(schema)
    return schema


def _fix_security_scheme_casing(schema: dict[str, Any]) -> None:
    """Work around quart-schema 0.19.0's `_SchemaBase.schema(camelize=True)` no-op.

    `humps.camelize(name)` is called for its return value but never assigned
    back (see quart_schema/openapi.py `_SchemaBase.schema`), so
    `HttpSecurityScheme.bearer_format` is emitted verbatim instead of the
    OpenAPI-3.x-required `bearerFormat` -- an invalid document per
    `oas3-schema` (spectral). Renamed in place rather than patched upstream
    since requirements.in exact-pins this version.
    """
    for scheme in schema.get("components", {}).get("securitySchemes", {}).values():
        if "bearer_format" in scheme:
            scheme["bearerFormat"] = scheme.pop("bearer_format")


def _referenced_schema_names(node: Any, found: set[str]) -> None:
    """Recursively collect `components.schemas` names reachable from *node* via `$ref`."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            _referenced_schema_names(value, found)
    elif isinstance(node, list):
        for item in node:
            _referenced_schema_names(item, found)


def build_public_schema(app: Quart) -> dict[str, Any]:
    """Build the public OpenAPI document: the login path and nothing else.

    This is the sole unauthenticated document allowed by backend.md OpenAPI
    -- a caller has to be able to discover how to authenticate before it has
    a token to authenticate with. Component schemas are filtered to only
    those the login path's request/response actually reference, so no
    detail about the authenticated surface leaks through `$defs`.
    """
    full = build_full_schema(app)
    login_item = full["paths"].get(LOGIN_PATH)
    paths = {LOGIN_PATH: login_item} if login_item is not None else {}

    referenced: set[str] = set()
    _referenced_schema_names(login_item, referenced)
    all_schemas = full.get("components", {}).get("schemas", {})
    components: dict[str, Any] = {
        "schemas": {k: v for k, v in all_schemas.items() if k in referenced},
    }
    security_schemes = full.get("components", {}).get("securitySchemes")
    if security_schemes:
        components["securitySchemes"] = security_schemes

    return {
        "openapi": full["openapi"],
        "info": full["info"],
        "components": components,
        "paths": paths,
    }


@openapi_bp.route("/api/v1/openapi/public.json", methods=["GET"])
async def public_openapi_spec():
    """Public OpenAPI document: the login endpoint only. Unauthenticated by design."""
    return jsonify(build_public_schema(current_app))


@openapi_bp.route("/api/v1/openapi/full.json", methods=["GET"])
@require_auth
async def full_openapi_spec():
    """Full OpenAPI document for every registered route. Requires authentication."""
    return jsonify(build_full_schema(current_app))


@openapi_bp.route("/api/v1/docs", methods=["GET"])
@require_auth
async def swagger_docs_ui():
    """Authenticated Swagger UI, rendered against the gated full spec."""
    return _SWAGGER_TEMPLATE.format(
        spec_url="/api/v1/openapi/full.json",
        css_sri=_SWAGGER_CSS_SRI,
        js_sri=_SWAGGER_JS_SRI,
    )
