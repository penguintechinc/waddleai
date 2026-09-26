"""PostHog-backed feature-flag client for the standalone PenguinCode service.

PenguinCode has no flag client today (docs/superpowers/specs/2026-09-25-
penguincode-knowledge-platform-design.md §10). This module adds one,
self-contained -- it deliberately does not import ``shared.utils.feature_flags``
or ``proxy.apps.proxy_server.feature_flag_cache`` from the WaddleAI monorepo
(penguincode stays standalone) -- but mirrors their resolution order and
graceful-degradation contract:

1. An env override (``PENGUINCODE_FLAG_<NAME>``) -- used by tests/alpha.
2. PostHog (self-hosted CE), when ``POSTHOG_KEY`` is configured.
3. Graceful degradation: PostHog configured but unreachable/erroring ->
   the last-known cached value for that (flag, caller) pair; never cached ->
   **OFF**. A flag that has simply never been defined in PostHog (client
   answers ``None``) is a deliberate default, not an outage, and also
   resolves OFF. The client never raises into the caller.

``ScopeContextLike`` is a structural (``Protocol``) stand-in for T2's
``penguincode_cli.auth.scope.ScopeContext`` so this module has no import-order
dependency on that task -- any object with the five ``ScopeContext`` fields
(tenant_id, org_id, team_ids, user_id, scopes) satisfies it, including the
real dataclass once T2 lands.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from posthog import Posthog

logger = logging.getLogger(__name__)

#: The four knowledge-platform flag keys (spec §10). Graph flags depend on
#: RAG_FLAG (graphs augment RAG) -- that dependency is enforced by callers
#: (T7/T11/T12/T13), not by this generic client.
RAG_FLAG = "penguincode.rag"
CODE_GRAPH_FLAG = "penguincode.code-graph"
KNOWLEDGE_GRAPH_FLAG = "penguincode.knowledge-graph"
MEMORY_GRAPH_FLAG = "penguincode.memory-graph"

_ENV_PREFIX = "PENGUINCODE_FLAG_"
_TRUTHY = ("1", "true", "yes", "on")
_DEFAULT_POSTHOG_HOST = "https://license.penguintech.io"


@runtime_checkable
class ScopeContextLike(Protocol):
    """Structural match for T2's ``ScopeContext`` -- see module docstring.

    Declared as read-only properties (not plain attributes) so a frozen
    dataclass -- exactly what T2's ``ScopeContext`` is per Shared Contracts --
    satisfies this protocol; mypy treats a plain mutable attribute as
    incompatible with a Protocol's mutable-attribute annotation.
    """

    @property
    def tenant_id(self) -> str: ...
    @property
    def org_id(self) -> str | None: ...
    @property
    def team_ids(self) -> tuple[str, ...]: ...
    @property
    def user_id(self) -> str: ...
    @property
    def scopes(self) -> tuple[str, ...]: ...


def _env_var_name(flag_key: str) -> str:
    """``penguincode.code-graph`` -> ``PENGUINCODE_FLAG_CODE_GRAPH``."""
    suffix = flag_key.split(".", 1)[-1]
    return _ENV_PREFIX + suffix.replace("-", "_").replace(".", "_").upper()


class _Outcome(Enum):
    """Classification of a single flag-store lookup."""

    RESOLVED = auto()  #: definite value from an env override or a PostHog answer
    UNRESOLVED = auto()  #: PostHog configured but the lookup raised -- an outage
    DEFAULTED = auto()  #: no flag store configured, or the flag is undefined


@dataclass(slots=True)
class FlagClient:
    """Cached, never-raising PostHog flag resolver for penguincode.

    One instance is safe to share process-wide (thread-safe cache); the
    module-level :func:`is_enabled` uses a lazily-constructed default
    instance for the common case of "just evaluate this flag".
    """

    _cache: dict[tuple[str, str], bool] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, compare=False)
    #: Test seam only -- production code always leaves this ``None`` and goes
    #: through the lazy, env-driven :meth:`_default_client_factory`.
    _client_factory: Callable[[], Posthog | None] | None = field(
        default=None, compare=False, repr=False
    )
    _posthog_client: Posthog | None = field(default=None, compare=False, repr=False)
    _posthog_client_built: bool = field(default=False, compare=False, repr=False)

    @staticmethod
    def _default_client_factory() -> Posthog | None:
        """Construct a PostHog client from env, or ``None`` if unconfigured."""
        api_key = os.getenv("POSTHOG_KEY")
        if not api_key:
            return None
        from posthog import Posthog

        return Posthog(api_key, host=os.getenv("POSTHOG_HOST", _DEFAULT_POSTHOG_HOST))

    def _get_client(self) -> Posthog | None:
        """Lazily build and cache the PostHog client (or the test double)."""
        if self._posthog_client_built:
            return self._posthog_client
        factory = self._client_factory or self._default_client_factory
        try:
            client = factory()
        except Exception as exc:  # noqa: BLE001 -- construction must never raise upward
            logger.warning("penguincode flag client construction failed: %s", exc)
            client = None
        self._posthog_client = client
        self._posthog_client_built = True
        return client

    def _resolve_raw(
        self, flag_key: str, distinct_id: str, ctx: ScopeContextLike
    ) -> tuple[_Outcome, bool]:
        """Resolve one flag lookup, never raising.

        Distinguishes a real PostHog outage (``UNRESOLVED`` -> degrade to
        last-known) from a deliberate default (``DEFAULTED`` -> no flag store
        configured, or the flag itself is undefined in PostHog).
        """
        env_val = os.getenv(_env_var_name(flag_key))
        if env_val is not None:
            return _Outcome.RESOLVED, env_val.strip().lower() in _TRUTHY

        client = self._get_client()
        if client is None:
            return _Outcome.DEFAULTED, False

        group_properties: dict[str, Any] = {}
        person_properties = {"user_id": ctx.user_id, "team_ids": list(ctx.team_ids)}
        groups = {"organization": ctx.org_id} if ctx.org_id else None

        try:
            result = client.feature_enabled(
                flag_key,
                distinct_id,
                groups=groups,
                person_properties=person_properties,
                group_properties=group_properties or None,
            )
        except Exception as exc:  # noqa: BLE001 -- outage: configured but unreachable
            logger.warning("penguincode flag %s lookup failed (PostHog outage): %s", flag_key, exc)
            return _Outcome.UNRESOLVED, False

        if result is None:
            return _Outcome.DEFAULTED, False
        return _Outcome.RESOLVED, bool(result)

    def _apply(self, key: tuple[str, str], outcome: _Outcome, value: bool) -> bool:
        """Turn a lookup outcome into a returned value, updating the cache."""
        flag_key, distinct_id = key

        if outcome is _Outcome.RESOLVED:
            with self._lock:
                self._cache[key] = value
            return value

        if outcome is _Outcome.DEFAULTED:
            # No flag store configured, or the flag is undefined -- deliberate,
            # not an outage. Never-seen flags default OFF (spec §10/§14).
            return False

        # UNRESOLVED == PostHog outage: degrade to last-known, else fail OFF.
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            logger.warning(
                "penguincode flag %s unresolvable (PostHog outage); using last-known "
                "cached value=%s for distinct_id=%s",
                flag_key,
                cached,
                distinct_id,
            )
            return cached

        logger.warning(
            "penguincode flag %s unresolvable (PostHog outage) and never cached; "
            "defaulting OFF for distinct_id=%s",
            flag_key,
            distinct_id,
        )
        return False

    def is_enabled(self, key: str, ctx: ScopeContextLike) -> bool:
        """Evaluate ``key`` for the caller in ``ctx``. Never raises.

        The tenant is the natural rollout unit (tenant is the hard boundary
        everywhere else in this platform), so ``ctx.tenant_id`` is the
        PostHog distinct id; user/team ride along as person properties for
        finer-grained targeting if a rollout needs it.
        """
        distinct_id = ctx.tenant_id
        cache_key = (key, distinct_id)
        try:
            outcome, value = self._resolve_raw(key, distinct_id, ctx)
        except Exception as exc:  # noqa: BLE001 -- absolute backstop, never raise into caller
            logger.warning("penguincode flag %s resolution raised unexpectedly: %s", key, exc)
            with self._lock:
                cached = self._cache.get(cache_key)
            return cached if cached is not None else False
        return self._apply(cache_key, outcome, value)


_default_client = FlagClient()


def is_enabled(key: str, ctx: ScopeContextLike) -> bool:
    """Module-level convenience matching the Shared Contract call shape.

    Callers (T7 docs-RAG, T11 code graph, T12 knowledge graph, T13 memory
    graph, T14 GraphRAG retrieval) use this directly: ``flags.client.is_enabled(key, ctx)``.
    """
    return _default_client.is_enabled(key, ctx)


__all__ = [
    "RAG_FLAG",
    "CODE_GRAPH_FLAG",
    "KNOWLEDGE_GRAPH_FLAG",
    "MEMORY_GRAPH_FLAG",
    "ScopeContextLike",
    "FlagClient",
    "is_enabled",
]
