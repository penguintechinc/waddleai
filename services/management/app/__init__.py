"""
WaddleAI Management Server - Flask Application Factory
Manages AI providers, Ollama deployments, usage tracking, and MarchProxy AILB integration
"""

import logging
import os
import time
from datetime import datetime

from flask import Flask, Response
from flask_cors import CORS

from .config import Config
from .extensions import db, init_extensions, redis_client, security

# Process start time for uptime metric
_START_TIME = time.time()


def _auto_register_k8s_ollama(app):
    """
    Auto-register the in-cluster Ollama DaemonSet when OLLAMA_HOST is set by Helm.

    When ollama.enabled=true in the Helm chart, OLLAMA_HOST is injected pointing
    to the waddleai-ollama ClusterIP Service. This registers it as a managed
    kubernetes-daemonset deployment so WaddleAI can track health and models.
    """
    ollama_host = os.environ.get("OLLAMA_HOST")
    mode = app.config.get("OLLAMA_MANAGEMENT_MODE", "both")

    if not ollama_host or mode == "manual":
        return

    with app.app_context():
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
                    "tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}],
                },
                resource_limits={"cpu_limit": "4", "memory_limit": "16Gi", "shared_storage_size": "200Gi"},
                status="running",
                health_status="unknown",
                auto_start=False,
                created_at=datetime.utcnow(),
            )
            _db.commit()
            app.logger.info(f"Auto-registered in-cluster Ollama DaemonSet at {ollama_host}")
        except Exception as e:
            app.logger.warning(f"Failed to auto-register Ollama deployment: {e}")


def create_app(config_class=Config):
    """Flask application factory"""
    app = Flask(__name__)
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
    CORS(
        app,
        resources={
            r"/api/*": {
                "origins": app.config.get("CORS_ORIGINS", ["*"]),
                "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
                "allow_headers": ["Content-Type", "Authorization", "X-Requested-With"],
            }
        },
    )

    # Register blueprints
    register_blueprints(app)

    # Register error handlers
    register_error_handlers(app)

    # Auto-register in-cluster Ollama when deployed via Helm with ollama.enabled=true
    _auto_register_k8s_ollama(app)

    # Health check endpoint
    @app.route("/healthz")
    def healthz():
        """Kubernetes-style health check - tolerant of transient DB issues"""
        # During startup, DB connections may not be immediately available in all workers
        # Return 200 if app is running, even if DB connection fails temporarily
        return "healthy", 200

    @app.route("/readyz")
    def readyz():
        """Kubernetes-style readiness check"""
        from . import extensions as _ext

        checks = {"database": False, "redis": False}

        try:
            from sqlalchemy import text

            with _ext.db.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            checks["database"] = True
        except Exception:
            pass

        try:
            if _ext.redis_client:
                _ext.redis_client.ping()
                checks["redis"] = True
        except Exception:
            pass

        all_ready = all(checks.values())
        return {"ready": all_ready, "checks": checks}, 200 if all_ready else 503

    @app.route("/livez")
    def livez():
        """Kubernetes-style liveness check — always 200 while process is running"""
        return "alive", 200

    @app.route("/metrics")
    def metrics():
        """Basic Prometheus-format metrics endpoint"""
        from . import extensions as _ext

        db_up = 0
        redis_up = 0

        try:
            from sqlalchemy import text

            with _ext.db.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            db_up = 1
        except Exception:
            pass

        try:
            if _ext.redis_client:
                _ext.redis_client.ping()
                redis_up = 1
        except Exception:
            pass

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
    """Register API blueprints"""
    from .api.v1 import api_v1_bp
    from .api.v1.routing_matrix import routing_matrix_bp

    app.register_blueprint(api_v1_bp, url_prefix="/api/v1")
    app.register_blueprint(routing_matrix_bp)

    app.logger.info("Registered API v1 blueprints")


def register_error_handlers(app):
    """Register error handlers"""
    from flask import jsonify

    @app.errorhandler(400)
    def bad_request(error):
        return (
            jsonify(
                {
                    "error": "Bad Request",
                    "message": str(error.description) if hasattr(error, "description") else str(error),
                }
            ),
            400,
        )

    @app.errorhandler(401)
    def unauthorized(error):
        return jsonify({"error": "Unauthorized", "message": "Authentication required"}), 401

    @app.errorhandler(403)
    def forbidden(error):
        return jsonify({"error": "Forbidden", "message": "Insufficient permissions"}), 403

    @app.errorhandler(404)
    def not_found(error):
        return jsonify({"error": "Not Found", "message": "Resource not found"}), 404

    @app.errorhandler(500)
    def internal_error(error):
        app.logger.error(f"Internal server error: {error}")
        return jsonify({"error": "Internal Server Error", "message": "An unexpected error occurred"}), 500
