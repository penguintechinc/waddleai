"""WaddleAI Management Server - Quart Application Factory.

Manages AI providers, Ollama deployments, usage tracking, and MarchProxy AILB integration.
"""

import asyncio
import logging
import os
import time
from datetime import datetime

import quart_cors
from quart import Quart, Response

from .config import Config
from .extensions import db, init_extensions, redis_client

# Process start time for uptime metric
_START_TIME = time.time()


def _auto_register_k8s_ollama_sync(app):
    """Auto-register the in-cluster Ollama DaemonSet when OLLAMA_HOST is set by Helm.

    When ollama.enabled=true in the Helm chart, OLLAMA_HOST is injected pointing
    to the waddleai-ollama ClusterIP Service. This registers it as a managed
    kubernetes-daemonset deployment so WaddleAI can track health and models.

    This performs blocking DB I/O and must only be invoked via
    `asyncio.to_thread(...)` from an async context (see `_auto_register_k8s_ollama`).
    """
    ollama_host = os.environ.get("OLLAMA_HOST")
    mode = app.config.get("OLLAMA_MANAGEMENT_MODE", "both")

    if not ollama_host or mode == "manual":
        return

    try:
        from .extensions import db as _db

        existing = _db(_db.ollama_deployments.endpoint_url == ollama_host).select().first()
        if existing:
            return

        _db.ollama_deployments.insert(
            name="k8s-gpu-pool",
            endpoint_url=ollama_host,
            deployment_type="kubernetes-daemonset",
            gpu_config={
                "gpu_count": 1,
                "node_selector": {"gpu": "true"},
                "tolerations": [
                    {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}
                ],
            },
            resource_limits={
                "cpu_limit": "4",
                "memory_limit": "16Gi",
                "shared_storage_size": "200Gi",
            },
            status="running",
            health_status="unknown",
            auto_start=False,
            created_at=datetime.utcnow(),
        )
        _db.commit()
        app.logger.info(f"Auto-registered in-cluster Ollama DaemonSet at {ollama_host}")
    except Exception as e:
        app.logger.warning(f"Failed to auto-register Ollama deployment: {e}")


def _bootstrap_cilium_reconcile_sync(app):
    """Run one bootstrap Cilium policy reconcile at startup.

    Non-blocking (invoked via asyncio.to_thread), swallow-and-log — startup
    must succeed even when Cilium is absent, the flag is off, or the k8s API
    is unreachable. The reconciler itself never raises; this wrapper exists
    only to guard against an unexpected import-time failure.
    """
    try:
        from .extensions import db as _db
        from .services.cilium_policy import CiliumPolicyReconciler

        status = CiliumPolicyReconciler(_db).reconcile()
        app.logger.info(
            "Bootstrap Cilium reconcile: skipped=%s reason=%s applied=%d degraded=%s",
            status.skipped,
            status.reason,
            len(status.applied),
            status.degraded,
        )
    except Exception as e:
        app.logger.warning(f"Bootstrap Cilium reconcile failed (non-fatal): {e}")


def create_app(config_class=Config):
    """Quart application factory."""
    app = Quart(__name__)
    app.config.from_object(config_class)

    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG if app.config["DEBUG"] else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    app.logger.info("Initializing WaddleAI Management Server")

    # Initialize extensions
    init_extensions(app)

    # Enable CORS
    app = quart_cors.cors(app, allow_origin=app.config.get("CORS_ORIGINS", ["*"]))

    # Register blueprints
    register_blueprints(app)

    # Register error handlers
    register_error_handlers(app)

    # Auto-register in-cluster Ollama when deployed via Helm with ollama.enabled=true
    @app.before_serving
    async def _auto_register_k8s_ollama():
        await asyncio.to_thread(_auto_register_k8s_ollama_sync, app)

    # Bootstrap Cilium policy reconcile (control-plane only; never in the
    # AIProxy request path). No-ops cleanly when the flag is off or Cilium
    # CRDs are absent (§12.3).
    @app.before_serving
    async def _bootstrap_cilium_reconcile():
        await asyncio.to_thread(_bootstrap_cilium_reconcile_sync, app)

    # Health check endpoint
    @app.route("/healthz")
    async def healthz():
        """Kubernetes-style health check - tolerant of transient DB issues."""
        # During startup, DB connections may not be immediately available in all workers
        # Return 200 if app is running, even if DB connection fails temporarily
        return "healthy", 200

    @app.route("/readyz")
    async def readyz():
        """Kubernetes-style readiness check."""
        from . import extensions as _ext

        checks = {"database": False, "redis": False}

        try:
            from sqlalchemy import text

            with _ext.db.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            checks["database"] = True
        except Exception as exc:
            # Expected/ignorable: readiness probe reports "not ready" on a down
            # dependency rather than raising -- the DB being unreachable IS the
            # signal this endpoint exists to report, not a bug to surface here.
            app.logger.debug("Readiness DB check failed: %s", exc)

        try:
            if _ext.redis_client:
                _ext.redis_client.ping()
                checks["redis"] = True
        except Exception as exc:
            # Expected/ignorable: same rationale as the DB check above.
            app.logger.debug("Readiness Redis check failed: %s", exc)

        all_ready = all(checks.values())
        return {"ready": all_ready, "checks": checks}, 200 if all_ready else 503

    @app.route("/livez")
    async def livez():
        """Kubernetes-style liveness check — always 200 while process is running."""
        return "alive", 200

    @app.route("/metrics")
    async def metrics():
        """Basic Prometheus-format metrics endpoint."""
        from . import extensions as _ext

        db_up = 0
        redis_up = 0

        try:
            from sqlalchemy import text

            with _ext.db.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            db_up = 1
        except Exception as exc:
            # Expected/ignorable: a down dependency reports as a 0-valued gauge,
            # not a scrape failure -- that is the metric's entire purpose.
            app.logger.debug("Metrics DB check failed: %s", exc)

        try:
            if _ext.redis_client:
                _ext.redis_client.ping()
                redis_up = 1
        except Exception as exc:
            # Expected/ignorable: same rationale as the DB check above.
            app.logger.debug("Metrics Redis check failed: %s", exc)

        uptime_seconds = time.time() - _START_TIME
        pid = os.getpid()

        lines = [
            "# HELP waddleai_up Management service availability",
            "# TYPE waddleai_up gauge",
            "waddleai_up 1",
            "# HELP waddleai_uptime_seconds Seconds since process start",
            "# TYPE waddleai_uptime_seconds counter",
            f"waddleai_uptime_seconds {uptime_seconds:.2f}",
            "# HELP waddleai_db_up Database connectivity (1=up, 0=down)",
            "# TYPE waddleai_db_up gauge",
            f"waddleai_db_up {db_up}",
            "# HELP waddleai_redis_up Redis connectivity (1=up, 0=down)",
            "# TYPE waddleai_redis_up gauge",
            f"waddleai_redis_up {redis_up}",
            "# HELP waddleai_process_pid Worker process ID",
            "# TYPE waddleai_process_pid gauge",
            f"waddleai_process_pid {pid}",
        ]
        return Response("\n".join(lines) + "\n", mimetype="text/plain; version=0.0.4")

    app.logger.info("WaddleAI Management Server initialized successfully")
    return app


def register_blueprints(app):
    """Register API blueprints."""
    from .api.v1 import (
        api_v1_bp,
        hook_rules,  # noqa: F401 -- registers admin CRUD routes onto hooks_bp
    )
    from .api.v1.hooks import hooks_bp
    from .api.v1.model_aliases import model_aliases_bp
    from .api.v1.routing_assignments import routing_assignments_bp
    from .api.v1.routing_decisions import routing_decisions_bp
    from .api.v1.routing_dry_run import routing_dry_run_bp
    from .api.v1.routing_policies import routing_policies_bp
    from .api.v1.routing_rules import routing_rules_bp
    from .api.v1.security_policies import security_policies_bp

    app.register_blueprint(api_v1_bp, url_prefix="/api/v1")
    app.register_blueprint(routing_assignments_bp)
    app.register_blueprint(routing_policies_bp)
    app.register_blueprint(routing_rules_bp)
    app.register_blueprint(model_aliases_bp)
    app.register_blueprint(routing_decisions_bp)
    app.register_blueprint(routing_dry_run_bp)
    app.register_blueprint(security_policies_bp)
    app.register_blueprint(hooks_bp)

    app.logger.info("Registered API v1 blueprints")


def register_error_handlers(app):
    """Register error handlers."""
    from quart import jsonify

    @app.errorhandler(400)
    async def bad_request(error):
        return (
            jsonify(
                {
                    "error": "Bad Request",
                    "message": str(error.description)
                    if hasattr(error, "description")
                    else str(error),
                }
            ),
            400,
        )

    @app.errorhandler(401)
    async def unauthorized(error):
        return jsonify({"error": "Unauthorized", "message": "Authentication required"}), 401

    @app.errorhandler(403)
    async def forbidden(error):
        return jsonify({"error": "Forbidden", "message": "Insufficient permissions"}), 403

    @app.errorhandler(404)
    async def not_found(error):
        return jsonify({"error": "Not Found", "message": "Resource not found"}), 404

    @app.errorhandler(500)
    async def internal_error(error):
        app.logger.error(f"Internal server error: {error}")
        return jsonify(
            {"error": "Internal Server Error", "message": "An unexpected error occurred"}
        ), 500
