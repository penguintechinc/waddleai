"""WaddleAI Management Service — ASGI entrypoint (hypercorn)."""

import os

from app import create_app

_env = os.environ.get("FLASK_ENV", "production")
if _env == "development":
    from app.config import DevelopmentConfig as _Cfg
elif _env == "testing":
    from app.config import TestingConfig as _Cfg
else:
    from app.config import ProductionConfig as _Cfg

app = create_app(_Cfg)
