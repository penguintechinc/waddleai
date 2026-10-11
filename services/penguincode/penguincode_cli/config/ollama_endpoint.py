"""Canonical "where is Ollama" resolution for PenguinCode.

Ops-audit finding (2026-10-09): "where is Ollama" was spelled five different
ways across the repo (``OLLAMA_URL``, ``OLLAMA_API_URL``, ``OLLAMA_HOST``,
``OLLAMA_BASE_URL``, ``OLLAMA_EMBEDDING_URL``, ``PENGUINCODE_EMBEDDING_OLLAMA_URL``)
-- several defaulting to ``localhost``, so a process that set one name still
silently landed on localhost via a different code path. This module is the
ONE place that resolution happens inside ``penguincode_cli``; every reader
(`config.settings.OllamaConfig`, the ``ollama_ready`` test fixture, etc.)
calls `resolve_ollama_url`/`resolve_ollama_embedding_url` instead of reading
any of the env vars directly.

PenguinCode ships its own ``shared/py_libs`` dependency tree and cannot
import this repo's root ``shared/`` package, so this is a deliberate, tested
twin of ``shared/utils/ollama_endpoint.py`` -- NOT an import of it.
``tests/unit/test_ollama_endpoint_parity.py`` (root) asserts the two
constant tuples below stay byte-identical to the root module's.

Resolution order (chat): `WADDLEAI_OLLAMA_URL` (canonical) -> legacy
`OLLAMA_API_URL` -> `OLLAMA_URL` -> `OLLAMA_HOST` -> `OLLAMA_BASE_URL` ->
``default``.

Resolution order (embedding): `WADDLEAI_OLLAMA_EMBEDDING_URL` (canonical) ->
legacy `OLLAMA_EMBEDDING_URL` -> `PENGUINCODE_EMBEDDING_OLLAMA_URL` -> the
resolved chat URL above.

No breaking change: every legacy name keeps working, at lower precedence
than the canonical name, with a one-time DEPRECATION warning naming the
canonical replacement.
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
#: proxy's own, undocumented sixth spelling found during the same audit pass
#: -- kept here too so the two modules' constant tuples stay identical (see
#: `tests/unit/test_ollama_endpoint_parity.py`), even though PenguinCode
#: itself never set it.
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

    Every chat-facing Ollama call site in PenguinCode must call this instead
    of reading any of the underlying env vars directly.
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
    no dedicated embedding endpoint (the bulkhead, ops-audit O10/O5) is
    configured -- today's single-Ollama behavior, unchanged by default.
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
