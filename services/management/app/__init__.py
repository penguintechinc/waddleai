"""
WaddleAI Management Server - Flask Application Factory
Manages AI providers, Ollama deployments, usage tracking, and MarchProxy AILB integration
"""

import logging

from flask import Flask
from flask_cors import CORS

from .config import Config
from .extensions import db, init_extensions, redis_client, security


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
        checks = {"database": False, "redis": False}

        try:
            from sqlalchemy import text

            with db.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            checks["database"] = True
        except Exception:
            pass

        try:
            if redis_client:
                redis_client.ping()
                checks["redis"] = True
        except Exception:
            pass

        all_ready = all(checks.values())
        return {"ready": all_ready, "checks": checks}, 200 if all_ready else 503

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
