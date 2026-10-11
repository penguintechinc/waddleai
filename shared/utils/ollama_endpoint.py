"""Canonical "where is Ollama" resolution for the proxy/shared Python tree.

Ops-audit finding (2026-10-09): "where is Ollama" was spelled five different
ways across the repo (``OLLAMA_URL``, ``OLLAMA_API_URL``, ``OLLAMA_HOST``,
``OLLAMA_BASE_URL``, ``OLLAMA_EMBEDDING_URL``, ``PENGUINCODE_EMBEDDING_OLLAMA_URL``)
-- several defaulting to ``localhost``, so a process that set one name still
silently fell back to localhost via a different code path reading another
name. This module is the ONE place that resolution happens for the
proxy/shared tree; every reader (``shared.utils.embedding_manager``, the
proxy's Ollama-backed security auditor in ``proxy_server.main``, etc.) calls
`resolve_ollama_url`/`resolve_ollama_embedding_url` instead of reading any of
the env vars directly.

PenguinCode cannot import this package (it ships its own ``shared/py_libs``
dependency tree, independent of this repo's root ``shared/``) -- it carries a
deliberate, tested twin at ``penguincode_cli/config/ollama_endpoint.py``;
``tests/unit/test_ollama_endpoint_parity.py`` asserts the two stay in
lockstep.

Resolution order (chat): `WADDLEAI_OLLAMA_URL` (canonical) -> legacy
`OLLAMA_API_URL` -> `OLLAMA_URL` -> `OLLAMA_HOST` -> `OLLAMA_BASE_URL` ->
``default``.

Resolution order (embedding): `WADDLEAI_OLLAMA_EMBEDDING_URL` (canonical) ->
legacy `OLLAMA_EMBEDDING_URL` -> `PENGUINCODE_EMBEDDING_OLLAMA_URL` -> the
resolved chat URL above (today's single-Ollama behavior when no dedicated
embedding endpoint is configured).

No breaking change: every legacy name keeps working, at lower precedence than
the canonical name, with a one-time DEPRECATION warning naming the canonical
replacement.
"""

from __future__ import annotations

import logging
import os
from typing import Final

logger = logging.getLogger(__name__)

#: Canonical env vars -- set these in new deployments.
CANONICAL_CHAT_URL_ENV: Final[str] = "WADDLEAI_OLLAMA_URL"
CANONICAL_EMBEDDING_URL_ENV: Final[str] = "WADDLEAI_OLLAMA_EMBEDDING_URL"

#: Legacy env vars, in fallback precedence order. `OLLAMA_BASE_URL` was the
#: proxy's own, undocumented sixth spelling (the Ollama-backed security
#: auditor in `proxy_server.main`) found during the same audit pass.
LEGACY_CHAT_URL_ENVS: Final[tuple[str, ...]] = (
    "OLLAMA_API_URL",
    "OLLAMA_URL",
    "OLLAMA_HOST",
    "OLLAMA_BASE_URL",
)
LEGACY_EMBEDDING_URL_ENVS: Final[tuple[str, ...]] = (
    "OLLAMA_EMBEDDING_URL",
    "PENGUINCODE_EMBEDDING_OLLAMA_URL",
)

DEFAULT_OLLAMA_URL: Final[str] = "http://localhost:11434"

#: One-time log-emission state. Private, module-level, and deliberately
#: mutable: tests that need to re-trigger the log (to assert it fires
#: exactly once) reset these directly, e.g. ``ollama_endpoint._logged_chat
#: = False``.
_logged_chat = False
_logged_embedding = False


def _first_set(names: tuple[str, ...]) -> tuple[str, str] | None:
    """Return the ``(env_var_name, value)`` of the first non-empty var in *names*."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return name, value
    return None


def _resolve(
    canonical_env: str, legacy_envs: tuple[str, ...], default: str, already_logged: bool
) -> tuple[str, str, bool]:
    """Shared resolution logic: canonical -> legacy chain -> default.

    Returns ``(value, source_env_name_or_"default", should_log)`` -- logging
    is the caller's responsibility since the two public resolvers log under
    different module-level "already logged" flags.
    """
    canonical_value = os.environ.get(canonical_env)
    if canonical_value:
        source, value = canonical_env, canonical_value
    else:
        found = _first_set(legacy_envs)
        source, value = found if found else ("default", default)
    return value, source, not already_logged


def resolve_ollama_url(default: str = DEFAULT_OLLAMA_URL) -> str:
    """Resolve the chat/completions Ollama base URL -- see module docstring.

    Every chat-facing Ollama call site (the proxy's security auditor, the
    embedding fallback below) must call this instead of reading any of the
    underlying env vars directly, so the resolution order stays centralized.
    """
    global _logged_chat
    value, source, should_log = _resolve(
        CANONICAL_CHAT_URL_ENV, LEGACY_CHAT_URL_ENVS, default, _logged_chat
    )
    if should_log:
        logger.info("Resolved Ollama chat URL %s from %s", value, source)
        if source not in (CANONICAL_CHAT_URL_ENV, "default"):
            logger.warning(
                "%s is DEPRECATED for the Ollama chat URL -- set %s instead",
                source,
                CANONICAL_CHAT_URL_ENV,
            )
        _logged_chat = True
    return value


def resolve_ollama_embedding_url(chat_url: str | None = None) -> str:
    """Resolve the Ollama base URL embedding calls should target.

    Falls back to *chat_url* (or `resolve_ollama_url()` if not supplied) when
    no dedicated embedding endpoint is configured -- today's single-Ollama
    behavior, unchanged unless an operator opts into the bulkhead.
    """
    global _logged_embedding
    default = chat_url if chat_url is not None else resolve_ollama_url()
    value, source, should_log = _resolve(
        CANONICAL_EMBEDDING_URL_ENV, LEGACY_EMBEDDING_URL_ENVS, default, _logged_embedding
    )
    if should_log:
        logger.info("Resolved Ollama embedding URL %s from %s", value, source)
        if source not in (CANONICAL_EMBEDDING_URL_ENV, "default"):
            logger.warning(
                "%s is DEPRECATED for the Ollama embedding URL -- set %s instead",
                source,
                CANONICAL_EMBEDDING_URL_ENV,
            )
        _logged_embedding = True
    return value


def embedding_endpoint_label(chat_url: str | None = None) -> str:
    """Bounded two-value metric label for which Ollama endpoint embedding calls target.

    Returns ``"embedding_ollama"`` when a dedicated bulkhead endpoint is
    configured (canonical or legacy), else ``"chat_ollama"``. Never the raw
    URL -- stays a safe, low-cardinality metric attribute.
    """
    canonical_or_legacy = os.environ.get(CANONICAL_EMBEDDING_URL_ENV) or _first_set(
        LEGACY_EMBEDDING_URL_ENVS
    )
    return "embedding_ollama" if canonical_or_legacy else "chat_ollama"
