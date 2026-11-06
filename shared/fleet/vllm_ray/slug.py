"""Deployment slug rules: one globally unique, path-safe id per vLLM deployment.

The slug names the Ray route (``/d/<slug>``), the model-store directory and the per-deployment
key file, so it must be a safe single path segment AND unique across ALL orgs (a shared slug
would share weights and bearer keys between tenants).
"""

from __future__ import annotations

import re
import uuid

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_NAME_MAX = 40
_ID_HEX = 12


def validate_slug(slug: object) -> str:
    """Return ``slug`` if it is a valid path-safe slug, else raise ``ValueError``."""
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise ValueError("invalid deployment slug: must match ^[a-z0-9][a-z0-9-]{0,62}$")
    return slug


def derive_slug(name: str, public_id: uuid.UUID | str) -> str:
    """``<sanitised name, max 40>-<first 12 hex of public_id>`` (globally unique via the id)."""
    hex_id = uuid.UUID(str(public_id)).hex[:_ID_HEX]
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:_NAME_MAX].strip("-")
    return validate_slug(f"{base}-{hex_id}" if base else hex_id)
