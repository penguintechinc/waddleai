"""ASGI entrypoint: ``hypercorn services.classification.asgi:app``."""

from __future__ import annotations

from services.classification.app import create_app

app = create_app()
