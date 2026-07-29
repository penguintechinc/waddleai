"""
ProxyPipeline stage classes with ordered execution and OpenTelemetry instrumentation.

Stages execute in cheapest-first order (§3.2):
  auth → token_budget → security_in → cache → routing → dispatch → security_out → meter

Each stage is independently testable, flag-aware, and emits structured span data.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, List, Optional

from shared.observability.tracing import get_tracer

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PipelineContext:
    """Request context threaded through pipeline stages."""

    user: Any
    body: dict
    model: Optional[str] = None
    messages: list = field(default_factory=list)
    prompt_text: str = ""
    response_text: str = ""
    usage: Optional[dict] = None
    blocked: bool = False
    block_reason: Optional[str] = None
    status_code: int = 200
    stream: bool = False
    stage_log: List[str] = field(default_factory=list)
    # Set by the dispatch stage; feeds the gen_ai.* span attributes (§15.3) and
    # lets routing report the actually-served model rather than the requested one.
    provider: Optional[str] = None
    requested_model: Optional[str] = None
    finish_reason: Optional[str] = None


class Stage(ABC):
    """Base class for pipeline stages."""

    def __init__(self, name: str, flag: Optional[str] = None) -> None:
        """
        Initialize a stage.

        Args:
            name: Stage name (e.g., 'auth', 'dispatch')
            flag: Optional feature flag to gate this stage (None = always run)
        """
        self.name = name
        self.flag = flag

    @abstractmethod
    async def __call__(self, ctx: PipelineContext) -> PipelineContext:
        """
        Execute stage logic.

        Args:
            ctx: Pipeline context

        Returns:
            Updated context (or blocked context if gated)
        """
        raise NotImplementedError


class ProxyPipeline:
    """Orchestrates stage execution with short-circuit and tracing."""

    def __init__(self, stages: List[Stage], features: Any) -> None:
        """
        Initialize pipeline.

        Args:
            stages: List of Stage instances in execution order
            features: Feature flag helper with is_feature_enabled(flag_key, distinct_id=...) method
        """
        self.stages = stages
        self.features = features
        self.tracer = get_tracer("waddleai-proxy-pipeline")

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """
        Execute all stages in order, short-circuiting if ctx.blocked is set.

        Stage-log is updated with:
        - 'ran:{stage_name}' if stage executed
        - 'skipped:{stage_name}' if flag was disabled
        - 'short-circuit:{stage_name}' if previous stage blocked

        Args:
            ctx: Pipeline context

        Returns:
            Final context after all stages (or short-circuit point)
        """
        with self.tracer.start_as_current_span("pipeline"):
            for stage in self.stages:
                # Check if stage is flag-gated
                if stage.flag is not None:
                    # Extract distinct_id from user if available
                    distinct_id = None
                    if hasattr(ctx.user, "id"):
                        distinct_id = str(ctx.user.id)

                    is_enabled = self.features.is_feature_enabled(
                        stage.flag,
                        distinct_id=distinct_id,
                    )
                    if not is_enabled:
                        ctx.stage_log.append(f"skipped:{stage.name}")
                        continue

                # Short-circuit if previous stage blocked
                if ctx.blocked:
                    ctx.stage_log.append(f"short-circuit:{stage.name}")
                    continue

                # Execute stage with tracing
                with self.tracer.start_as_current_span(stage.name) as span:
                    try:
                        ctx = await stage(ctx)
                        ctx.stage_log.append(f"ran:{stage.name}")
                        if stage.name == "dispatch":
                            self._set_genai_attributes(span, ctx)
                    except Exception as e:
                        logger.error(f"Stage {stage.name} failed: {e}", exc_info=True)
                        span.set_attribute("error", True)
                        span.set_attribute("error.message", str(e))
                        # Don't block the pipeline on stage errors; let them propagate
                        raise

        return ctx

    @staticmethod
    def _set_genai_attributes(span: Any, ctx: PipelineContext) -> None:
        """Attach GenAI semantic-convention attributes to the dispatch span.

        Only model identifiers and token counts — never prompt or completion
        content, credentials, or PII (spec §15.3). Emitted names follow the
        OpenTelemetry GenAI conventions, which are still Development status
        upstream, so treat them as subject to change.
        """
        provider = getattr(ctx, "provider", None)
        if provider:
            span.set_attribute("gen_ai.system", provider)

        requested_model = getattr(ctx, "requested_model", None) or ctx.model
        if requested_model:
            span.set_attribute("gen_ai.request.model", requested_model)
        if ctx.model:
            span.set_attribute("gen_ai.response.model", ctx.model)

        usage = ctx.usage or {}
        if usage.get("input_tokens") is not None:
            span.set_attribute("gen_ai.usage.input_tokens", int(usage["input_tokens"]))
        if usage.get("output_tokens") is not None:
            span.set_attribute("gen_ai.usage.output_tokens", int(usage["output_tokens"]))

        finish_reason = getattr(ctx, "finish_reason", None)
        if finish_reason:
            span.set_attribute("gen_ai.response.finish_reason", finish_reason)


class AuthStage(Stage):
    """Authenticate user and validate token."""

    async def __call__(self, ctx: PipelineContext) -> PipelineContext:
        """Extract and validate user from context."""
        # User already set by middleware; just log
        logger.debug(f"Auth: user={getattr(ctx.user, 'id', None)}")
        return ctx


class TokenBudgetStage(Stage):
    """Check TPM and monthly token/USD budgets."""

    async def __call__(self, ctx: PipelineContext) -> PipelineContext:
        """
        Reserve tokens from budget.

        Sets ctx.blocked = True if budget exceeded.
        """
        # Placeholder: actual implementation wires TokenLimiter.reserve
        # This stage will be wired in Task 11
        return ctx


class SecurityInStage(Stage):
    """Scan input prompts for injection/jailbreak threats."""

    async def __call__(self, ctx: PipelineContext) -> PipelineContext:
        """
        Scan messages with PromptSecurityScanner and ContentFilter.filter_input.

        Sets ctx.blocked = True if threat detected.
        """
        # Placeholder: actual implementation wires PromptSecurityScanner + ContentFilter
        # Ordering: prompt_security FIRST (fail fast), content_filter SECOND (PII redaction)
        # This stage will be wired in Task 11
        return ctx


class CacheStage(Stage):
    """Check for cached completions (placeholder for later branch)."""

    async def __call__(self, ctx: PipelineContext) -> PipelineContext:
        """Cache lookup and hit/miss handling."""
        # No-op placeholder; wired in future cache branch
        return ctx


class RoutingStage(Stage):
    """Route request to optimal provider."""

    async def __call__(self, ctx: PipelineContext) -> PipelineContext:
        """
        Use LLMRequestRouter to select provider.

        Sets ctx.model from request_body if not already set.
        """
        # No-op placeholder; wired in future routing branch
        # Actual implementation wires request_router logic
        return ctx


class DispatchStage(Stage):
    """Call upstream LLM provider and capture response."""

    async def __call__(self, ctx: PipelineContext) -> PipelineContext:
        """
        Route to provider via llm_connectors, capture usage.

        Dispatch span wraps the provider call so provider latency
        is separable from proxy overhead.

        Sets:
        - ctx.response_text: completion from provider
        - ctx.usage: {"input_tokens": N, "output_tokens": M}
        - ctx.stream: True if streaming response
        """
        # No-op placeholder; actual implementation wires provider connectors
        # This stage will be wired in Task 11
        return ctx


class SecurityOutStage(Stage):
    """Filter response output for PII/sensitive data."""

    async def __call__(self, ctx: PipelineContext) -> PipelineContext:
        """
        Run ContentFilter.filter_output on ctx.response_text.

        Redacts PII from LLM response before returning to user.
        """
        # No-op placeholder; wired in Task 11
        # Implementation calls ContentFilter.filter_output
        return ctx


class MeterStage(Stage):
    """Record token usage to metering buffer."""

    async def __call__(self, ctx: PipelineContext) -> PipelineContext:
        """
        Write ctx.usage to MeteringBuffer, reconcile TokenLimiter reservation.

        Numerically validates gen_ai.usage tokens from dispatch span
        match metered usage.
        """
        # No-op placeholder; wired in Task 11
        # Implementation uses MeteringBuffer.record and TokenLimiter.reconcile
        return ctx
