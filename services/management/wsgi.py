"""
WaddleAI Management Service - WSGI Entry Point
"""

import os

from app import create_app

# Determine configuration based on environment
config_name = os.environ.get("FLASK_ENV", "production")

if config_name == "development":
    from app.config import DevelopmentConfig

    app = create_app(DevelopmentConfig)
elif config_name == "testing":
    from app.config import TestingConfig

    app = create_app(TestingConfig)
else:
    from app.config import ProductionConfig

    app = create_app(ProductionConfig)


if __name__ == "__main__":
    # Development server only
    app.run(host="0.0.0.0", port=8001, debug=(config_name == "development"))
