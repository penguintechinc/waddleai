"""The failover dispatcher -- walks an ordered destination list (spec S5.3/S5.4).

Retryable failure (`is_retryable`) advances to the next destination and trips the
per-destination breaker; a client error (4xx) propagates immediately without failing
over and without touching the breaker. A breaker-open destination (no half-open probe
available) or a registry `OwnershipError`/`ValueError` (credential/config defect) is
skipped without being counted as a failure. One attempt is one connector call bounded
by `dest.timeout_seconds` (total time for non-streaming, time-to-first-chunk for
streaming); the same filtered `messages` list is passed to every attempt, unmutated,
and each attempt opens a fresh stream generator. Failover is legal only before the
first flushed byte of THIS request (`ctx.bytes_flushed`) -- a retryable failure after
that point re-raises instead of trying the next destination.

Exception taxonomy at the connector boundary: `asyncio.TimeoutError` (from the
`asyncio.wait_for` bound above) becomes `ProviderTimeoutError`; a genuine transport
failure escaping the connector -- `OSError` (covers `ConnectionError`,
`ConnectionRefusedError`, socket timeouts), `aiohttp.ClientError`, or
`httpx.TransportError` when `httpx` is importable -- becomes a retryable
`ProviderServerError` carrying only `type(exc).__name__`, never the exception's own
text. Any other `ProviderError` (already typed by the connector) passes through
unchanged. Every OTHER exception type (`KeyError`, `AttributeError`, `RuntimeError`,
...) is a bug, not a provider failure, and propagates unchanged out of `dispatch` --
no failover, no breaker trip, no attempt record beyond what already happened.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from shared.routing.destination_breaker import DestinationBreaker
from shared.routing.destination_connectors import DestinationConnectorRegistry, OwnershipError
from shared.routing.destinations import Destination
from shared.utils.llm_connectors import (
    ProviderError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
    classify_failure,
    is_retryable,
)
from shared.utils.metrics import WaddleAIMetrics

try:
    import httpx
except ImportError:  # pragma: no cover -- httpx is a repo dependency; guarded defensively
    httpx = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 30
_STATUS_BY_REASON = {"rate_limit": 429, "timeout": 504, "server_error": 502}

# Genuine transport-layer failures only -- anything else (KeyError, AttributeError,
# RuntimeError, ...) is a bug and must propagate unchanged, never be treated as a
# retryable provider failure.
_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (OSError, aiohttp.ClientError)
if httpx is not None:
    _TRANSPORT_ERRORS = (*_TRANSPORT_ERRORS, httpx.TransportError)


@dataclass(slots=True, frozen=True)
class DestinationAttempt:
    """One entry in a dispatch's attempt trail: one destination, one outcome."""

    destination_id: int
    provider: str
    outcome: str  # "ok" | "failed" | "skipped" | "client_error"
    reason: str | None = None


@dataclass(slots=True, frozen=True)
class Outcome:
    """A successful destination result plus the full attempt trail (spec S5.7)."""

    destination: Destination
    text: str
    usage: dict[str, Any] = field(repr=False, default_factory=dict)
    finish_reason: str = "stop"
    attempts: tuple[DestinationAttempt, ...] = ()

    @property
    def marker(self) -> dict[str, Any]:
        """usage.waddleai.destination payload -- ids/roles only, never a URL or secret."""
        return {
            "id": self.destination.id,
            "priority": self.destination.priority,
            "role": self.destination.role,
            "provider": self.destination.provider_type,
            "model": self.destination.model,
            "attempts": [
                {
                    "destination_id": a.destination_id,
                    "provider": a.provider,
                    "outcome": a.outcome,
                    "reason": a.reason,
                }
                for a in self.attempts
            ],
        }


class DestinationsExhausted(Exception):  # noqa: N818 -- interface name fixed by spec (S5.3/S5.7)
    """Every destination was skipped or failed retryably; carries the last retryable error."""

    def __init__(
        self, attempts: tuple[DestinationAttempt, ...], last_error: ProviderError | None
    ) -> None:
        """Carry the attempt trail and the last retryable error for status/retry mapping."""
        super().__init__("all destinations exhausted")
        self.attempts = attempts
        self.last_error = last_error

    def status_code(self) -> int:
        """429/504/502 from the last retryable error's reason (502 if none was ever attempted)."""
        if self.last_error is None:
            return 502
        return _STATUS_BY_REASON.get(classify_failure(self.last_error), 502)

    def retry_after(self) -> float | None:
        """The last error's Retry-After (seconds), iff it was a rate limit."""
        if isinstance(self.last_error, ProviderRateLimitError):
            return self.last_error.retry_after
        return None


class FailoverDispatcher:
    """Ordered active/standby dispatch with breaker, first-byte, and bounded-attempt semantics."""

    def __init__(
        self,
        registry: DestinationConnectorRegistry,
        breaker: DestinationBreaker,
        *,
        metrics: WaddleAIMetrics | None = None,
    ) -> None:
        """Bind the connector registry, breaker, and optional metrics sink."""
        self._registry = registry
        self._breaker = breaker
        self._metrics = metrics

    async def dispatch(
        self, ctx: Any, destinations: list[Destination], messages: list[Any]
    ) -> Outcome:
        """Try destinations in priority order; return the first success or raise (spec S5.3)."""
        attempts: list[DestinationAttempt] = []
        last_error: ProviderError | None = None
        pending_failover: tuple[str, str] | None = None  # (from_provider, reason)

        for dest in destinations:
            if pending_failover is not None:
                self._record_failover(pending_failover[0], dest.provider_type, pending_failover[1])
                pending_failover = None

            if not self._breaker.reserve_probe(dest.id):
                attempts.append(
                    DestinationAttempt(dest.id, dest.provider_type, "skipped", "breaker_open")
                )
                self._record(dest.provider_type, "skipped")
                pending_failover = (dest.provider_type, "breaker_open")
                continue

            try:
                connector = await self._registry.get(dest)
            except (OwnershipError, ValueError) as exc:
                # Ids and exception class only -- never the exception's own text, which
                # for a ValueError decrypt failure could echo back key material.
                logger.error(
                    "failover: destination %d skipped as config_defect (%s)",
                    dest.id,
                    type(exc).__name__,
                )
                attempts.append(
                    DestinationAttempt(dest.id, dest.provider_type, "skipped", "config_defect")
                )
                self._record(dest.provider_type, "skipped")
                pending_failover = (dest.provider_type, "config_defect")
                continue

            try:
                text, usage, finish = await self._attempt(connector, dest, messages, ctx)
            except ProviderError as exc:
                if not is_retryable(exc):
                    self._record(dest.provider_type, "client_error")
                    raise
                reason = classify_failure(exc)
                self._breaker.record_failure(dest.id)
                self._sync_breaker_gauge(dest.id)
                attempts.append(DestinationAttempt(dest.id, dest.provider_type, "failed", reason))
                self._record(dest.provider_type, "failed")
                if getattr(ctx, "bytes_flushed", False):
                    raise  # first-byte rule (spec S5.4) -- no failover past the first byte
                last_error = exc
                pending_failover = (dest.provider_type, reason)
                continue

            self._breaker.record_success(dest.id)
            self._sync_breaker_gauge(dest.id)
            attempts.append(DestinationAttempt(dest.id, dest.provider_type, "ok", None))
            self._record(dest.provider_type, "ok")
            return Outcome(
                destination=dest,
                text=text,
                usage=usage,
                finish_reason=finish,
                attempts=tuple(attempts),
            )

        raise DestinationsExhausted(tuple(attempts), last_error)

    async def _attempt(
        self, connector: Any, dest: Destination, messages: list[Any], ctx: Any
    ) -> tuple[str, dict[str, Any], str]:
        """One bounded connector call; normalises timeouts/transport errors to ProviderError."""
        target_model = dest.provider_model_id or ctx.model
        timeout = (
            dest.timeout_seconds if dest.timeout_seconds is not None else _DEFAULT_TIMEOUT_SECONDS
        )
        try:
            if getattr(ctx, "stream", False):
                return await self._attempt_stream(connector, target_model, messages, timeout, dest)
            text, usage = await asyncio.wait_for(
                connector.chat_completion(messages, model=target_model), timeout=timeout
            )
            usage = usage or {}
            return text, usage, usage.get("finish_reason", "stop")
        except TimeoutError as exc:
            raise ProviderTimeoutError(dest.provider_type, target_model, "attempt timeout") from exc
        except ProviderError:
            raise
        except _TRANSPORT_ERRORS as exc:  # genuine transport/connection error -> retryable
            raise ProviderServerError(
                dest.provider_type, target_model, f"connection error: {type(exc).__name__}"
            ) from exc

    async def _attempt_stream(
        self,
        connector: Any,
        target_model: str,
        messages: list[Any],
        timeout_seconds: int,
        dest: Destination,
    ) -> tuple[str, dict[str, Any], str]:
        """Bound only the time-to-first-chunk; drain the rest, accumulating text and usage."""
        gen = connector.stream_chat_completion(messages, model=target_model)
        try:
            first = await asyncio.wait_for(gen.__anext__(), timeout=timeout_seconds)
            text = first.delta or ""
            usage = first.usage if first.done and first.usage else None
            async for chunk in gen:
                text += chunk.delta or ""
                if chunk.done and chunk.usage:
                    usage = chunk.usage
        finally:
            aclose = getattr(gen, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()
        usage = usage or {}
        return text, usage, usage.get("finish_reason", "stop")

    def _record(self, provider_type: str, outcome: str) -> None:
        """Count one destination attempt outcome, if a metrics sink is bound."""
        if self._metrics is not None:
            self._metrics.record_destination_attempt(provider_type, outcome)

    def _record_failover(self, from_provider: str, to_provider: str, reason: str) -> None:
        """Count one failover hop between consecutive destinations, if metrics is bound."""
        if self._metrics is not None:
            self._metrics.record_destination_failover(from_provider, to_provider, reason)

    def _sync_breaker_gauge(self, dest_id: int) -> None:
        """Publish this destination's current breaker open/closed state, if metrics is bound."""
        if self._metrics is not None:
            self._metrics.set_destination_breaker_open(str(dest_id), self._breaker.is_open(dest_id))
