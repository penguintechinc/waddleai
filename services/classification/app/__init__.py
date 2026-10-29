"""Classification service: standalone PII-NER (Presidio/spaCy) behind gRPC."""

from __future__ import annotations

from quart import Quart

from .config import ClassificationConfig, ConfigError

__all__ = ["ClassificationConfig", "ConfigError", "create_app"]


def create_app(config: ClassificationConfig | None = None) -> Quart:
    """Build the Quart app, loading env-only config unless one is supplied.

    The HTTP surface (health/readiness) is added in later tasks; this is the factory.
    """
    app = Quart(__name__)
    app.config["CLASSIFICATION"] = config if config is not None else ClassificationConfig.from_env()
    return app
