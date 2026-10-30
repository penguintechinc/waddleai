"""gRPC ClassificationService: Analyze / GetEntityConfig orchestration.

Wires the engine, mapper, cache, tier gate and fail-mode policy behind the frozen
v1 contract. Responses carry spans only; request text, entity text and flag ids
are never logged.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

import grpc

from services.classification.app import engine as eng
from services.classification.app.engine import RawEntity
from services.classification.app.failmode import PolicyOutcome
from services.classification.app.grpc_gen import classification_pb2 as _pb
from services.classification.app.grpc_gen import classification_pb2_grpc as _pb_grpc
from services.classification.app.mapping import entity_type_for, map_spans
from services.classification.app.obs import get_logger
from services.classification.app.telemetry import SPAN_ANALYZE, Telemetry, get_telemetry
from shared.security.ner_filter import ENTITY_CONFIG

# protoc gencode ships no stubs; typed Any so mypy --strict accepts the dynamic modules.
pb: Any = _pb
_ServicerBase: Any = _pb_grpc.ClassificationServiceServicer

log = get_logger(__name__)

SUPPORTED_API_VERSION = "v1"
SUPPORTED_LANGUAGE = "en"
DEFAULT_DEADLINE_SECONDS = 30.0  # engine budget when the caller sets no gRPC deadline

# Engine failure -> (DegradedReason, NerStatus). Ordered subclass-safe: all siblings.
_ENGINE_ERRORS: tuple[tuple[type[eng.EngineError], int, int], ...] = (
    (
        eng.EngineUnavailable,
        pb.DEGRADED_REASON_BACKEND_UNAVAILABLE,
        pb.NER_STATUS_BACKEND_UNAVAILABLE,
    ),
    (eng.EngineOverloaded, pb.DEGRADED_REASON_OVERLOADED, pb.NER_STATUS_DEGRADED),
    (eng.EngineTimeout, pb.DEGRADED_REASON_TIMEOUT, pb.NER_STATUS_DEGRADED),
    (eng.EngineWorkerFailure, pb.DEGRADED_REASON_WORKER_FAILURE, pb.NER_STATUS_DEGRADED),
    (eng.EngineAnalysisError, pb.DEGRADED_REASON_ANALYSIS_ERROR, pb.NER_STATUS_DEGRADED),
)  # fmt: skip
_ACTIONS = {
    "log": pb.ENTITY_ACTION_LOG,
    "redact": pb.ENTITY_ACTION_REDACT,
    "block": pb.ENTITY_ACTION_BLOCK,
}


class _Engine(Protocol):
    """Engine surface the servicer needs."""

    @property
    def backend(self) -> str:
        """Active backend name."""
        ...

    @property
    def fingerprint(self) -> str:
        """Cache-key fingerprint."""
        ...

    async def analyze(self, text: str, language: str, deadline_s: float) -> list[RawEntity]:
        """Analyse text, raising Engine* errors."""
        ...


class _Cache(Protocol):
    """Cache surface the servicer needs."""

    def key(self, fingerprint: str, org_public_id: str, text: str) -> str:
        """Build the per-tenant key."""
        ...

    async def get(self, key: str) -> list[RawEntity] | None:
        """Look up raw entities."""
        ...

    async def put(self, key: str, raw: list[RawEntity]) -> None:
        """Store raw entities."""
        ...


class _Gate(Protocol):
    """Gate surface the servicer needs."""

    async def enabled(self, flag_distinct_id: str) -> bool:
        """Whether the NER tier may run."""
        ...


class _Policy(Protocol):
    """Fail-mode policy surface the servicer needs."""

    async def apply(
        self,
        *,
        phase: int,
        org_public_id: str,
        flag_distinct_id: str,
        requested_mode: int,
        reason: int,
    ) -> PolicyOutcome:
        """Resolve degraded/block for a failed analysis."""
        ...


@dataclass(slots=True)
class _Obs:
    """Per-request facts collected for telemetry (counts/enums only, never text)."""

    outcome: str = "invalid"
    cache_hit: bool = False
    ner_status: str = "UNKNOWN"
    entity_count: int = 0


class ClassificationServicer(_ServicerBase):  # type: ignore[misc]  # protoc base is untyped Any
    """Orchestrates validate -> gate -> cache -> engine -> map -> cache put."""

    def __init__(
        self,
        engine: _Engine,
        cache: _Cache,
        gate: _Gate,
        policy: _Policy,
        max_text_bytes: int,
        telemetry: Telemetry | None = None,
    ) -> None:
        """Inject collaborators, the text size limit (UTF-8 bytes) and optional telemetry."""
        self._engine = engine
        self._cache = cache
        self._gate = gate
        self._policy = policy
        self._max_text_bytes = max_text_bytes
        self._tel = telemetry if telemetry is not None else get_telemetry()
        depth = getattr(engine, "queue_depth", None)
        if depth is not None:
            self._tel.bind_queue_source(lambda: int(getattr(self._engine, "queue_depth", 0)))

    async def Analyze(self, request: Any, context: Any) -> Any:  # noqa: N802 - gRPC name
        """Detect PII spans in ``request.text``; never returns the text itself.

        Wraps ``_analyze`` in the ``classification.analyze`` span (joined to the caller's
        trace via gRPC metadata) and emits request/duration metrics for every outcome.
        """
        obs = _Obs()
        started = time.monotonic()
        parent = self._tel.extract_context(context)
        with self._tel.tracer.start_as_current_span(SPAN_ANALYZE, context=parent) as span:
            try:
                response = await self._analyze(request, context, obs)
            except grpc.aio.AbortError:
                obs.outcome = "invalid"
                raise
            finally:
                self._tel.record_request(obs.outcome)
                self._tel.observe_duration(time.monotonic() - started)
                self._tel.set_attributes(
                    span,
                    {
                        "cache.hit": obs.cache_hit,
                        "ner.backend": str(self._engine.backend),
                        "ner.status": obs.ner_status,
                        "ner.entity_count": obs.entity_count,
                        "text.chars": len(request.text),
                    },
                )
        return response

    async def _analyze(self, request: Any, context: Any, obs: _Obs) -> Any:
        """Validate -> gate -> cache -> engine -> map; records telemetry facts in ``obs``."""
        await self._check_version(request.api_version, context)
        if request.language != SUPPORTED_LANGUAGE:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "language must be 'en'")
        if len(request.text.encode("utf-8", errors="surrogatepass")) > self._max_text_bytes:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "text exceeds maximum size")
        if not request.text:
            obs.outcome, obs.ner_status = "ok", "OK"
            return pb.AnalyzeResponse(ner_status=pb.NER_STATUS_OK, backend=self._engine.backend)
        if not await self._gate.enabled(request.flag_distinct_id):
            obs.outcome, obs.ner_status = "gate_disabled", "GATE_DISABLED"
            return pb.AnalyzeResponse(
                ner_status=pb.NER_STATUS_GATE_DISABLED, backend=self._engine.backend
            )

        fingerprint = self._engine.fingerprint
        # Empty org would collapse all unidentified callers into one shared cache
        # partition (cross-tenant oracle), so such requests bypass the cache entirely.
        cache_key = (
            self._cache.key(fingerprint, request.org_public_id, request.text)
            if request.org_public_id
            else None
        )
        errors_before = self._cache_errors()
        raw = await self._cache.get(cache_key) if cache_key is not None else None
        cache_hit = raw is not None
        if cache_key is not None:
            errored = self._cache_errors() > errors_before
            self._tel.record_cache("hit" if cache_hit else "error" if errored else "miss")
        obs.cache_hit = cache_hit
        if raw is None:
            try:
                raw = await self._run_engine(request, context)
            except eng.EngineError as exc:
                obs.outcome = "degraded"
                response = await self._degraded(request, exc)
                obs.ner_status = str(pb.NerStatus.Name(response.ner_status)).removeprefix(
                    "NER_STATUS_"
                )
                return response
            if cache_key is not None:
                errors_before = self._cache_errors()
                await self._cache.put(cache_key, raw)
                if self._cache_errors() > errors_before:
                    self._tel.record_cache("error")

        mapped = map_spans(request.text, raw, frozenset(request.disabled_entity_labels))
        obs.outcome, obs.ner_status, obs.entity_count = "ok", "OK", len(mapped.spans)
        self._tel.record_entities(mapped.spans)
        return pb.AnalyzeResponse(
            spans=mapped.spans,
            ner_status=pb.NER_STATUS_OK,
            backend=self._engine.backend,
            cache_hit=cache_hit,
            analyzed_chars=mapped.analyzed_chars,
            truncated=mapped.truncated,
            model_fingerprint=fingerprint,
        )

    async def GetEntityConfig(self, request: Any, context: Any) -> Any:  # noqa: N802 - gRPC name
        """Return the entity table (type, action, weight) for client drift contract tests."""
        await self._check_version(request.api_version, context)
        items = []
        for label, (action, weight) in ENTITY_CONFIG.items():
            enum_value, _ = entity_type_for(label)
            items.append(
                pb.EntityConfigItem(
                    entity_type=enum_value,
                    entity_label=label,
                    default_action=_ACTIONS.get(action, pb.ENTITY_ACTION_LOG),
                    confidence_weight=weight,
                )
            )
        return pb.GetEntityConfigResponse(items=items)

    def _cache_errors(self) -> int:
        """Read the cache's cumulative error counter (0 for caches that do not expose one)."""
        value = getattr(self._cache, "cache_error", 0)
        return value if isinstance(value, int) else 0

    @staticmethod
    async def _check_version(version: str, context: Any) -> None:
        """Abort UNIMPLEMENTED unless ``version`` is the supported api_version."""
        if version != SUPPORTED_API_VERSION:
            await context.abort(
                grpc.StatusCode.UNIMPLEMENTED, f"api_version {version} not supported"
            )

    async def _run_engine(self, request: Any, context: Any) -> list[RawEntity]:
        """Run the engine within the caller's remaining deadline (spent = EngineTimeout)."""
        remaining = context.time_remaining() if hasattr(context, "time_remaining") else None
        deadline = DEFAULT_DEADLINE_SECONDS if remaining is None else float(remaining)
        if deadline <= 0:
            raise eng.EngineTimeout("deadline already expired")
        return await self._engine.analyze(request.text, request.language, deadline)

    async def _degraded(self, request: Any, exc: eng.EngineError) -> Any:
        """Build the degraded response for an engine failure and apply the fail-mode policy."""
        reason, status = pb.DEGRADED_REASON_ANALYSIS_ERROR, pb.NER_STATUS_DEGRADED
        for err_type, err_reason, err_status in _ENGINE_ERRORS:
            if isinstance(exc, err_type):
                reason, status = err_reason, err_status
                break
        outcome = await self._policy.apply(
            phase=request.phase,
            org_public_id=request.org_public_id,
            flag_distinct_id=request.flag_distinct_id,
            requested_mode=request.fail_mode,
            reason=reason,
        )
        log.warning("classification_engine_error", error_type=type(exc).__name__)
        return pb.AnalyzeResponse(
            ner_status=status,
            backend=self._engine.backend,
            degraded=outcome.degraded,
            degraded_reason=reason,
            block_recommended=outcome.block_recommended,
        )
