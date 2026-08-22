"""Shared licence client + feature-flag accessor for ``ContentFilter`` call sites.

Every ``app.api.v1`` route that constructs a ``ContentFilter`` (``hooks.py``,
``knowledge.py``, ``memory_scoping.py``) wires the NER tier's flag+licence
gate through this module's process-wide singletons instead of opening a new
``penguin_licensing.LicenseClient`` per call.
"""

from __future__ import annotations

import os
from typing import Any

import shared.utils.feature_flags as feature_flags

_license_client: Any = None


def get_content_filter_license_client() -> Any:
    """Lazily construct and cache the shared ``penguin_licensing.LicenseClient``.

    Mirrors ``app/api/v1/fleet.py``'s ``_get_license_client`` -- one client
    per process (``product="waddleai"``), reused across every
    ``ContentFilter`` construction site rather than opened per call.
    """
    global _license_client
    if _license_client is None:
        from penguin_licensing import LicenseClient

        _license_client = LicenseClient(
            license_key=os.environ.get("LICENSE_KEY", ""),
            product="waddleai",
            base_url=os.environ.get("LICENSE_SERVER_URL", "https://license.penguintech.io"),
        )
    return _license_client


def get_content_filter_features() -> Any:
    """Return the feature-flag helper for ``ContentFilter``'s ``features`` kwarg.

    ``shared.utils.feature_flags`` already exposes a module-level
    ``is_feature_enabled(flag_key, distinct_id, default)`` matching the
    duck-typed ``features.is_feature_enabled(...)`` contract ``ContentFilter``
    expects, so the module itself is returned rather than wrapping it in a
    redundant adapter class.
    """
    return feature_flags
