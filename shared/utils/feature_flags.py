"""PostHog-backed feature flags with graceful degradation.

Evaluation order:
  1. Environment override ``WADDLEAI_FLAG_<NAME>`` ("1"/"true"/"yes"/"on"
     enables; anything else disables) — used by tests and alpha.
  2. PostHog, when ``POSTHOG_KEY`` is configured (host defaults to the
     centralized license server).
  3. The caller-supplied default (OFF for new flags, per house rules).

Any PostHog failure falls back to the default — flag evaluation must never
raise into request handling.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_posthog_client: Optional[object] = None

_TRUTHY = ("1", "true", "yes", "on")


def _env_var_name(flag_key: str) -> str:
    """waddleai.memory-org-scope -> WADDLEAI_FLAG_MEMORY_ORG_SCOPE."""
    suffix = flag_key.split(".", 1)[-1]
    return "WADDLEAI_FLAG_" + suffix.replace("-", "_").replace(".", "_").upper()


def _get_posthog_client() -> Optional[object]:
    """Lazily construct and cache the PostHog client (None if unconfigured)."""
    global _posthog_client
    api_key = os.getenv("POSTHOG_KEY")
    if not api_key:
        return None
    if _posthog_client is None:
        from posthog import Posthog

        _posthog_client = Posthog(
            api_key,
            host=os.getenv("POSTHOG_HOST", "https://license.penguintech.io"),
        )
    return _posthog_client


def is_feature_enabled(flag_key: str, distinct_id: str = "server", default: bool = False) -> bool:
    """Evaluate a feature flag. Never raises; falls back to ``default``."""
    env_val = os.getenv(_env_var_name(flag_key))
    if env_val is not None:
        return env_val.strip().lower() in _TRUTHY

    try:
        client = _get_posthog_client()
        if client is None:
            return default
        result = client.feature_enabled(flag_key, distinct_id)
        return default if result is None else bool(result)
    except Exception as exc:
        logger.warning("Feature flag %s evaluation failed, using default=%s: %s", flag_key, default, exc)
        return default
