"""Managed MCP workload control plane (Wave 5)."""

from __future__ import annotations

import asyncio
from typing import Any

from quart import current_app, g, jsonify, request

from shared.auth.rbac import Permission
from shared.mcp.kubernetes_driver import KubernetesManagedMcpDriver
from shared.mcp.managed import ManagedMcpLifecycle, ManagedMcpSpec, ManagedMcpValidationError
from shared.utils.feature_flags import is_feature_enabled

from ... import extensions
from . import api_v1_bp
from .auth import require_auth, require_scope


def _driver() -> Any:
    driver = current_app.extensions.get("managed_mcp_driver")
    if driver is not None:
        return driver
    discover = current_app.extensions.get("managed_mcp_discover")
    if not callable(discover):
        raise RuntimeError("managed MCP discovery adapter is not configured")
    return KubernetesManagedMcpDriver(discover=discover)


def _db() -> Any:
    if extensions.db is None:
        raise RuntimeError("database not initialized")
    return extensions.db


@api_v1_bp.route("/integrations/managed-mcp", methods=["POST"])
@require_auth
@require_scope(Permission.INTEGRATION_ADMIN)
async def create_managed_mcp() -> tuple:
    """Provision a managed MCP server from the request body.

    Validates the spec, drives the lifecycle and returns the created record.
    """
    org_id = int(g.user["organization_id"])
    if not is_feature_enabled("waddleai.mcp_v2", distinct_id=str(org_id)):
        return jsonify({"error": "MCP feature disabled"}), 404
    body = await request.get_json() or {}
    try:
        spec = ManagedMcpSpec(
            name=str(body.get("name", "")),
            image=str(body.get("image", "")),
            namespace=str(body.get("namespace", "")),
            transport=str(body.get("transport", "streamable_http")),
        )
        lifecycle = ManagedMcpLifecycle(
            _driver(), allowed_namespaces=set(current_app.config.get("MANAGED_MCP_NAMESPACES", []))
        )
        endpoint = await lifecycle.provision(spec)
    except (ManagedMcpValidationError, RuntimeError, TimeoutError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        current_app.logger.exception("managed MCP provision failed")
        return jsonify({"error": f"managed MCP provisioning failed: {exc}"}), 502

    def _persist() -> tuple[int, int]:
        db = _db()
        endpoint_id = db.mcp_endpoints.insert(
            org_id=org_id,
            name=spec.name,
            url=endpoint.url,
            transport=endpoint.transport,
            auth_type="none",
            auth_config={},
            identity_mode="shared",
            namespace=spec.name,
            credentials_ref=None,
            status="active",
        )
        server_id = db.managed_mcp_servers.insert(
            org_id=org_id,
            endpoint_id=endpoint_id,
            name=spec.name,
            namespace=spec.namespace,
            image=spec.image,
            transport=spec.transport,
            status="active",
            discovered_tools=list(endpoint.discovered_tools),
        )
        db.commit()
        return server_id, endpoint_id

    try:
        server_id, endpoint_id = await asyncio.to_thread(_persist)
    except Exception:
        await lifecycle.cleanup(spec)
        raise
    return jsonify(
        {
            "status": "success",
            "data": {
                "id": server_id,
                "endpoint_id": endpoint_id,
                "url": endpoint.url,
                "tools": list(endpoint.discovered_tools),
            },
        }
    ), 201


@api_v1_bp.route("/integrations/managed-mcp/<int:server_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.INTEGRATION_ADMIN)
async def delete_managed_mcp(server_id: int) -> tuple:
    """Tear down a managed MCP server by id.

    Cleans up its workload and removes the stored record.
    """
    org_id = int(g.user["organization_id"])
    if not is_feature_enabled("waddleai.mcp_v2", distinct_id=str(org_id)):
        return jsonify({"error": "MCP feature disabled"}), 404

    def _read() -> Any:
        db = _db()
        return (
            db((db.managed_mcp_servers.id == server_id) & (db.managed_mcp_servers.org_id == org_id))
            .select()
            .first()
        )

    row = await asyncio.to_thread(_read)
    if row is None:
        return jsonify({"error": "managed MCP server not found"}), 404
    spec = ManagedMcpSpec(
        name=row.name, image=row.image, namespace=row.namespace, transport=row.transport
    )
    try:
        await ManagedMcpLifecycle(
            _driver(), allowed_namespaces=set(current_app.config.get("MANAGED_MCP_NAMESPACES", []))
        ).cleanup(spec)
    except (ManagedMcpValidationError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400

    def _delete() -> None:
        db = _db()
        db(db.managed_mcp_servers.id == server_id).delete()
        if row.endpoint_id:
            db(db.mcp_endpoints.id == row.endpoint_id).delete()
        db.commit()

    await asyncio.to_thread(_delete)
    return jsonify({"status": "success", "data": {"id": server_id}}), 200
