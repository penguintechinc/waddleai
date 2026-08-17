"""
ProxyPipeline stage classes with ordered execution and OpenTelemetry instrumentation.

Standard Execution Order (§3.2, cheapest-first):
  auth → token_budget → security_in → [CACHE_INSERTION_POINT] → dispatch →
    → security_out → meter

Each stage is independently testable, flag-aware, and emits structured span data.

Future Insertion Points:
  - CacheStage: Between security_in and dispatch (scheduled release §6).
    Implements prompt caching and completion reuse.
    Check cache before dispatch, store cache after dispatch (spec §6).

Landed:
  - RoutingStage (§7, flag waddleai.smart_routing): between security_in and
    dispatch, after any CacheStage slot. Passes ctx.model through as
    RoutingInput.requested_model, then resolves stage-0 model aliasing ->
    tool type -> model assignment -> capability veto -> policy fallback
    chain -> escalation -> sensitivity -> budget pressure via
    shared.routing.RoutingEngine, setting
    ctx.model/ctx.fallback_chain/ctx.routed_from. DispatchStage still owns
    concrete-endpoint selection (§7.5) and now also consumes
    ctx.fallback_chain for chaos failover.

Removed:
  - No more empty placeholder stages; insertion points are documented above.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from shared.observability.tracing import get_tracer
from shared.routing.aliases import explicit_tool_type
from shared.routing.capability import ModelOffer
from shared.routing.engine import RoutingEngine, RoutingInput
from shared.routing.heuristics import HeuristicRule, RequestSignals
from shared.security.content_filter import ContentFilter
from shared.security.prompt_security import PromptSecurityScanner
from shared.utils.llm_connectors import (
    LLMConnector,
    ProviderClientError,
    ProviderError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
)
from shared.utils.metering import MeteringBuffer, MeteringEvent
from shared.utils.request_router import LLMRequestRouter
from shared.utils.token_limiter import TokenLimiter

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
    # Stashed by TokenBudgetStage for MeterStage to reconcile
    reservation_id: Optional[str] = None
    # Set by SecurityInStage when input PII/sensitive content was detected
    # (redacted or not); consumed by RoutingStage's sensitivity clamp (§7.3).
    pii_detected: bool = False
    # Set by RoutingStage (§7): the policy-sorted qualified candidate chain,
    # excluding the chosen ctx.model, consumed by DispatchStage for failover.
    fallback_chain: list[str] = field(default_factory=list)
    # Set by RoutingStage when an alias/escalation/capability-veto redirect
    # occurred; surfaced to the client as usage.waddleai.routed_from (§7.6).
    routed_from: dict | None = None
    # Read by RoutingStage stage-0 cascade (§7.2): the X-WaddleAI-Tool-Type
    # header value, threaded through by the caller (main.py) at ctx
    # construction time rather than read from quart.request inside the
    # stage, so RoutingStage stays unit-testable without a request context.
    explicit_tool_type_hint: str | None = None
    # Read by RoutingStage escalation trigger 4 (§7.3): X-WaddleAI-Escalate
    # or an "auto:high"/"auto:low" model suffix, same threading rationale.
    escalate_hint: str | None = None


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
    """Authenticate user and validate tenant/organization context."""

    async def __call__(self, ctx: PipelineContext) -> PipelineContext:
        """
        Validate that ctx.user is present with valid organization/tenant.

        Middleware already authenticated the user; this stage ensures a valid
        organizational context exists for multi-tenant isolation.
        """
        # User must be present
        if ctx.user is None:
            ctx.blocked = True
            ctx.status_code = 401
            ctx.block_reason = "unauthenticated"
            logger.warning("Auth failed: user is None")
            return ctx

        # User must have an organization/tenant ID
        # Support both tenant_id (generic) and organization_id (WaddleAI UserContext)
        tenant_id = getattr(ctx.user, "tenant_id", None) or getattr(ctx.user, "organization_id", None)
        if not tenant_id:
            ctx.blocked = True
            ctx.status_code = 403
            ctx.block_reason = "missing_organization"
            user_id = getattr(ctx.user, "id", None) or getattr(ctx.user, "user_id", "?")
            logger.warning(f"Auth failed: user {user_id} has no tenant_id/organization_id")
            return ctx

        user_id = getattr(ctx.user, "id", None) or getattr(ctx.user, "user_id", "?")
        logger.debug(f"Auth: user={user_id} tenant={tenant_id}")
        return ctx


class TokenBudgetStage(Stage):
    """Check TPM and monthly token/USD budgets."""

    def __init__(self, name: str, token_limiter: TokenLimiter, features: Any, flag: Optional[str] = None) -> None:
        """
        Initialize TokenBudgetStage.

        Args:
            name: Stage name
            token_limiter: TokenLimiter instance for budget enforcement
            features: Feature flag helper
            flag: Optional feature flag to gate this stage
        """
        super().__init__(name, flag)
        self.token_limiter = token_limiter
        self.features = features

    async def __call__(self, ctx: PipelineContext) -> PipelineContext:
        """
        Reserve tokens from budget via TokenLimiter.

        Estimates input tokens, calls reserve, and stashes reservation_id
        for MeterStage to reconcile with actual usage. Sets ctx.blocked if
        budget exceeded (tpm, monthly tokens, or monthly usd).
        """
        if not ctx.user or not hasattr(ctx.user, "vkey_id"):
            logger.debug("TokenBudgetStage: skipping (no vkey_id)")
            return ctx

        vkey_id = ctx.user.vkey_id
        # Estimate input tokens (simplified: ~4 chars per token)
        estimated_input = sum(len(m.get("content", "")) // 4 for m in ctx.messages) or 1
        # Conservative estimate: 2x input for output
        estimated_output = estimated_input * 2
        total_estimated = estimated_input + estimated_output

        # Get limits for this key (mock scenario; in production fetch from DB/config)
        limits = getattr(ctx.user, "limits", None)
        if not limits:
            logger.debug("TokenBudgetStage: no limits configured for vkey %s", vkey_id)
            return ctx

        # Reserve tokens atomically
        decision = await self.token_limiter.reserve(
            vkey_id=vkey_id,
            estimated_tokens=total_estimated,
            estimated_usd=0.0,  # USD estimate would come from model pricing
            limits=limits,
        )

        if not decision.allowed:
            ctx.blocked = True
            ctx.status_code = 429
            ctx.block_reason = decision.reason
            logger.warning("TokenBudgetStage: quota exceeded for vkey %s: %s", vkey_id, decision.reason)
            return ctx

        # Stash reservation ID for reconciliation in MeterStage
        ctx.reservation_id = decision.reservation_id
        logger.debug(
            "TokenBudgetStage: reserved %d tokens for vkey %s (resv=%s)",
            total_estimated,
            vkey_id,
            decision.reservation_id,
        )
        return ctx


class SecurityInStage(Stage):
    """Scan input prompts for injection/jailbreak threats and PII/PCI."""

    def __init__(
        self,
        name: str,
        scanner: PromptSecurityScanner,
        content_filter: ContentFilter,
        flag: Optional[str] = None,
    ) -> None:
        """
        Initialize SecurityInStage.

        Args:
            name: Stage name
            scanner: PromptSecurityScanner instance
            content_filter: ContentFilter instance
            flag: Optional feature flag to gate this stage
        """
        super().__init__(name, flag)
        self.scanner = scanner
        self.content_filter = content_filter

    async def __call__(self, ctx: PipelineContext) -> PipelineContext:
        """
        Scan messages for threats and PII, in order of fail-fast.

        1. PromptSecurityScanner: checks for injection/jailbreak/data-extraction attacks
           (BLOCKS immediately on detection — fail fast)
        2. ContentFilter.filter_input: redacts PII/PCI from messages
           (updated messages are written back to ctx.messages)

        Returns blocked context if threat detected, otherwise filtered context.
        """
        if not ctx.messages:
            return ctx

        # STEP 1: Prompt security scan (fail fast on injection attacks)
        # Support both id (generic) and user_id (WaddleAI UserContext)
        user_id = getattr(ctx.user, "id", None) or getattr(ctx.user, "user_id", None)
        api_key_id = getattr(ctx.user, "api_key_id", None)
        ip_address = None  # Would come from request context

        threats, _ = self.scanner.scan_messages(
            ctx.messages,
            user_id=user_id,
            api_key_id=api_key_id,
            ip_address=ip_address,
        )

        if self.scanner.should_block(threats):
            ctx.blocked = True
            ctx.status_code = 400
            ctx.block_reason = "prompt_injection_detected"
            logger.warning("SecurityInStage: prompt injection blocked for user %s", user_id)
            return ctx

        # STEP 2: Content filter for PII/PCI redaction (only if no injection detected)
        # Process each message and update with filtered version
        # Support both tenant_id (generic) and organization_id (WaddleAI UserContext)
        org_id = getattr(ctx.user, "tenant_id", None) or getattr(ctx.user, "organization_id", None)
        filtered_messages = []
        pii_detected = False
        for msg in ctx.messages:
            content = msg.get("content", "")
            filter_result = await self.content_filter.filter_input(
                text=content,
                user_id=user_id,
                org_id=org_id,
                ip=ip_address,
            )

            if not filter_result.allowed:
                ctx.blocked = True
                ctx.status_code = 400
                ctx.block_reason = "pii_detected"
                logger.warning("SecurityInStage: PII detected in message for user %s", user_id)
                return ctx

            # Any violation (even a redacted, allowed one) flags the request
            # for RoutingStage's sensitivity clamp (§7.3) -- distinct from
            # ctx.blocked, which only fires on a hard block above.
            if filter_result.violations:
                pii_detected = True

            # Update message with filtered (redacted) content
            filtered_messages.append({**msg, "content": filter_result.filtered_text})

        ctx.messages = filtered_messages
        ctx.pii_detected = pii_detected
        logger.debug("SecurityInStage: scanned %d messages for user %s", len(ctx.messages), user_id)
        return ctx


class RoutingStage(Stage):
    """Stage 5: unified smart routing (spec §7).

    Resolves tool type (explicit/heuristic/classifier cascade), the model
    assignment, capability veto, org policy fallback chain, escalation,
    sensitivity clamp, and budget pressure via RoutingEngine, then sets
    ctx.model/ctx.fallback_chain/ctx.routed_from for DispatchStage. Flag-gated
    (waddleai.smart_routing) -- when off, this stage is skipped entirely by
    ProxyPipeline.run() and ctx.model is left exactly as determine_target_model
    set it (§14.2 flag-off byte-identical proof).
    """

    def __init__(
        self,
        name: str,
        engine: RoutingEngine,
        db: Any,
        rules: list[HeuristicRule] | None = None,
        flag: str | None = None,
    ) -> None:
        """Initialize RoutingStage.

        Args:
            name: Stage name
            engine: RoutingEngine facade composing the full §7 decision
            db: penguin-dal DB instance exposing model_configs (candidate offers)
            rules: Org routing_rules_v2 rows for the stage-1 heuristic cascade
            flag: Optional feature flag to gate this stage
        """
        super().__init__(name, flag)
        self.engine = engine
        self.db = db
        self.rules = rules or []

    async def __call__(self, ctx: PipelineContext) -> PipelineContext:
        """Run the RoutingEngine and apply its decision to ctx.

        Never blocks the request on routing ambiguity -- RoutingEngine
        always returns a usable model, falling back to ctx.model unchanged
        if something goes wrong loading candidate offers.
        """
        org_id_raw = getattr(ctx.user, "tenant_id", None) or getattr(
            ctx.user, "organization_id", None
        )
        try:
            org_id = int(org_id_raw) if org_id_raw is not None else 0
        except (TypeError, ValueError):
            org_id = 0

        explicit = explicit_tool_type(header_value=ctx.explicit_tool_type_hint, model=ctx.model)
        signals = RequestSignals(model=ctx.model or "")

        try:
            offers = await self._load_offers()
        except Exception as exc:  # pragma: no cover - defensive, DB I/O failure
            logger.warning(
                "RoutingStage: failed to load candidate offers, leaving ctx.model unchanged: %s",
                exc,
            )
            return ctx

        request_id = getattr(ctx.user, "request_id", None) or f"{id(ctx):x}"
        routing_input = RoutingInput(
            org_id=org_id,
            request_id=str(request_id),
            body=ctx.body,
            explicit_tool_type=explicit,
            requested_model=ctx.model,
            signals=signals,
            rules=self.rules,
            offers=offers,
            pii_detected=ctx.pii_detected,
            explicit_escalate_hint=ctx.escalate_hint,
        )

        try:
            decision = await self.engine.decide(routing_input)
        except Exception as exc:  # pragma: no cover - defensive, routing must never break dispatch
            logger.error(
                "RoutingStage: decide() failed, leaving ctx.model unchanged: %s", exc, exc_info=True
            )
            return ctx

        ctx.model = decision.model
        ctx.fallback_chain = decision.fallback_chain
        ctx.routed_from = decision.routed_from
        return ctx

    async def _load_offers(self) -> list[ModelOffer]:
        """Build the candidate ModelOffer universe from model_configs (penguin-dal).

        Interim capability source until migration 008 (model_registry) lands
        -- location is inferred from preferred_providers, cost from the mean
        of the per-provider cost_per_token map.
        """
        rows = await asyncio.to_thread(
            lambda: self.db(self.db.model_configs.enabled == True).select()  # noqa: E712
        )
        offers: list[ModelOffer] = []
        for row in rows:
            providers = row.preferred_providers or []
            is_local = any(p in ("ollama", "llamacpp") for p in providers)
            location = "local" if is_local else "commercial"
            costs = list((row.cost_per_token or {}).values())
            avg_cost = sum(costs) / len(costs) if costs else 0.0
            capabilities = row.capabilities or []
            offers.append(
                ModelOffer(
                    model_name=row.model_name,
                    context_window=row.context_length or 4096,
                    cost_per_token=avg_cost,
                    location=location,
                    supports_tools=True,
                    supports_vision="vision" in capabilities,
                )
            )
        return offers


class DispatchStage(Stage):
    """Call upstream LLM provider and capture response."""

    def __init__(
        self,
        name: str,
        router: LLMRequestRouter,
        connectors: Dict[str, LLMConnector],
        flag: Optional[str] = None,
    ) -> None:
        """
        Initialize DispatchStage.

        Args:
            name: Stage name
            router: LLMRequestRouter for provider selection
            connectors: Dict mapping provider name to LLMConnector instance
            flag: Optional feature flag to gate this stage
        """
        super().__init__(name, flag)
        self.router = router
        self.connectors = connectors

    async def __call__(self, ctx: PipelineContext) -> PipelineContext:
        """
        Route to provider and dispatch request.

        1. Select provider via router
        2. Call connector (streaming or non-streaming based on ctx.stream)
        3. Accumulate response and usage
        4. Map provider errors to HTTP status codes
        5. Populate ctx.response_text, ctx.usage, ctx.provider, ctx.model, ctx.finish_reason

        Non-retryable errors (4xx) and retries-exhausted errors (502/503) set ctx.blocked.
        """
        if not ctx.messages:
            ctx.blocked = True
            ctx.status_code = 400
            ctx.block_reason = "no_messages"
            return ctx

        # Select provider and target model via the router's public seam, which
        # applies availability filtering (and therefore the circuit breaker,
        # including its half-open probe). Reaching into the private helpers
        # here would let a caller bypass breaker semantics.
        #
        # ctx.fallback_chain (§7: populated only when RoutingStage ran, i.e.
        # empty when the flag is off) is consulted only if the primary model
        # has no available provider -- this is the chaos-failover path, not a
        # general retry loop; DispatchStage still calls the router exactly
        # once per candidate model.
        try:
            model = ctx.model or "gpt-4"
            selection = self.router.select_provider(model)
            if not selection:
                for fallback_model in ctx.fallback_chain:
                    selection = self.router.select_provider(fallback_model)
                    if selection:
                        model = fallback_model
                        break
            if not selection:
                logger.error("DispatchStage: no available providers for model %s", model)
                ctx.blocked = True
                ctx.status_code = 503
                ctx.block_reason = "no_available_providers"
                return ctx
            provider, target_model = selection
        except Exception as e:
            logger.error("DispatchStage: provider selection failed: %s", e)
            ctx.blocked = True
            ctx.status_code = 500
            ctx.block_reason = "routing_error"
            return ctx

        connector = self.connectors.get(provider)
        if not connector:
            logger.error("DispatchStage: no connector for provider %s", provider)
            ctx.blocked = True
            ctx.status_code = 500
            ctx.block_reason = "no_connector"
            return ctx

        ctx.provider = provider
        ctx.requested_model = ctx.model
        ctx.model = target_model

        try:
            if ctx.stream:
                # Streaming: accumulate chunks
                ctx.response_text = ""
                usage: Optional[Dict[str, Any]] = None
                async for chunk in connector.stream_chat_completion(ctx.messages, model=target_model):
                    ctx.response_text += chunk.delta
                    if chunk.done and chunk.usage:
                        usage = chunk.usage
                if usage:
                    ctx.usage = usage
                    ctx.finish_reason = usage.get("finish_reason", "stop")
            else:
                # Non-streaming: single call
                response_text, usage_info = await connector.chat_completion(ctx.messages, model=target_model)
                ctx.response_text = response_text
                ctx.usage = usage_info
                ctx.finish_reason = usage_info.get("finish_reason", "stop")

            logger.debug(
                "DispatchStage: dispatched to %s/%s (tokens: in=%s, out=%s)",
                provider,
                target_model,
                ctx.usage.get("input_tokens") if ctx.usage else "?",
                ctx.usage.get("output_tokens") if ctx.usage else "?",
            )

        except ProviderClientError as e:
            # 4xx: not retryable, pass through
            ctx.blocked = True
            ctx.status_code = e.status_code or 400
            ctx.block_reason = f"provider_error_{e.status_code}"
            logger.warning("DispatchStage: provider client error from %s: %s", provider, e)

        except (ProviderTimeoutError, ProviderRateLimitError, ProviderServerError) as e:
            # Retryable errors: already retried by connector, exhausted attempts
            # Map to appropriate HTTP error
            if isinstance(e, ProviderRateLimitError):
                ctx.status_code = 429
            elif isinstance(e, ProviderTimeoutError):
                ctx.status_code = 504
            else:
                # Server error (5xx)
                ctx.status_code = 502  # Bad Gateway (provider unreachable)

            ctx.blocked = True
            ctx.block_reason = f"provider_error_{e.status_code}"
            logger.warning("DispatchStage: provider error from %s (retries exhausted): %s", provider, e)

        except ProviderError as e:
            # Generic provider error
            ctx.blocked = True
            ctx.status_code = e.status_code or 500
            ctx.block_reason = "provider_error"
            logger.warning("DispatchStage: provider error from %s: %s", provider, e)

        except Exception as e:
            # Unexpected error
            logger.error("DispatchStage: unexpected error: %s", e, exc_info=True)
            ctx.blocked = True
            ctx.status_code = 500
            ctx.block_reason = "dispatch_error"

        return ctx


class SecurityOutStage(Stage):
    """Filter response output for PII/sensitive data."""

    def __init__(
        self,
        name: str,
        content_filter: ContentFilter,
        flag: Optional[str] = None,
    ) -> None:
        """
        Initialize SecurityOutStage.

        Args:
            name: Stage name
            content_filter: ContentFilter instance
            flag: Optional feature flag to gate this stage
        """
        super().__init__(name, flag)
        self.content_filter = content_filter

    async def __call__(self, ctx: PipelineContext) -> PipelineContext:
        """
        Filter LLM response for PII/PCI before returning to user.

        Redacts sensitive data from ctx.response_text. If filter denies the
        response (e.g., contains API keys), sets ctx.blocked.
        """
        if not ctx.response_text:
            return ctx

        # Support both id (generic) and user_id (WaddleAI UserContext)
        user_id = getattr(ctx.user, "id", None) or getattr(ctx.user, "user_id", None)
        # Support both tenant_id (generic) and organization_id (WaddleAI UserContext)
        org_id = getattr(ctx.user, "tenant_id", None) or getattr(ctx.user, "organization_id", None)
        ip_address = None  # Would come from request context

        try:
            filter_result = await self.content_filter.filter_output(
                text=ctx.response_text,
                user_id=user_id,
                org_id=org_id,
                ip=ip_address,
            )
        except (TypeError, AttributeError) as e:
            # Programming error at the call boundary (wrong kwargs, wrong
            # attribute) -- NOT a transient failure. ContentFilter._filter()
            # already fails open internally for genuine runtime/IO errors
            # (network, DB, LLM auditor unreachable), so anything reaching
            # here is a code defect that would otherwise silently disable
            # output PII filtering for every response (see bug: a stale
            # `ip=` kwarg made every call raise TypeError, and this branch
            # used to swallow it identically to a filter timeout). Fail
            # CLOSED and log loudly so this class of bug can never again
            # be indistinguishable from an ordinary fail-open path.
            logger.error(
                "SecurityOutStage: filter_output call is broken (%s: %s) for user %s -- "
                "this is a code defect, not a transient failure; blocking response.",
                type(e).__name__,
                e,
                user_id,
                exc_info=True,
            )
            ctx.blocked = True
            ctx.status_code = 500
            ctx.block_reason = "output_filter_defect"
            return ctx
        except Exception as e:
            logger.error("SecurityOutStage: filter error for user %s: %s", user_id, e, exc_info=True)
            # Fail open: don't block the response on genuine runtime/IO
            # errors reaching this far (ContentFilter._filter() already
            # fail-opens internally for the expected transient cases).
            return ctx

        if not filter_result.allowed:
            ctx.blocked = True
            ctx.status_code = 400
            ctx.block_reason = "response_blocked_pii"
            logger.warning("SecurityOutStage: response blocked due to PII for user %s", user_id)
            return ctx

        # Update response with redacted version
        ctx.response_text = filter_result.filtered_text
        logger.debug(
            "SecurityOutStage: filtered output for user %s (violations=%d)",
            user_id,
            len(filter_result.violations),
        )

        return ctx


class MeterStage(Stage):
    """Record token usage to metering buffer and reconcile budget reservation."""

    def __init__(
        self,
        name: str,
        metering_buffer: MeteringBuffer,
        token_limiter: TokenLimiter,
        flag: Optional[str] = None,
    ) -> None:
        """
        Initialize MeterStage.

        Args:
            name: Stage name
            metering_buffer: MeteringBuffer instance for batching writes
            token_limiter: TokenLimiter for reconciliation
            flag: Optional feature flag to gate this stage
        """
        super().__init__(name, flag)
        self.metering_buffer = metering_buffer
        self.token_limiter = token_limiter

    async def __call__(self, ctx: PipelineContext) -> PipelineContext:
        """
        Record actual token usage and reconcile budget reservation.

        This stage runs EVEN IF ctx.blocked is True (e.g., if provider returned
        an error after we sent tokens). Metering must be accurate for billing
        and quota enforcement, regardless of request outcome.

        1. If usage exists and provider was called: record to metering buffer
        2. If reservation was made: reconcile with actual usage
        """
        vkey_id = getattr(ctx.user, "vkey_id", None) if ctx.user else None
        if not vkey_id:
            logger.debug("MeterStage: no vkey_id, skipping")
            return ctx

        # Record usage if provider call occurred
        if ctx.usage and ctx.provider and ctx.model:
            event = MeteringEvent(
                virtual_key_id=vkey_id,
                model=ctx.model,
                provider=ctx.provider,
                usage=ctx.usage,
                timestamp=datetime.utcnow(),
                estimated=False,  # Actual usage from provider
            )
            self.metering_buffer.record(event)
            logger.debug(
                "MeterStage: recorded usage for vkey=%s model=%s tokens_in=%s tokens_out=%s",
                vkey_id,
                ctx.model,
                ctx.usage.get("input_tokens", "?"),
                ctx.usage.get("output_tokens", "?"),
            )

        # Reconcile budget reservation with actual usage if one was made
        if ctx.reservation_id and ctx.usage:
            input_tokens = ctx.usage.get("input_tokens", 0)
            output_tokens = ctx.usage.get("output_tokens", 0)
            total_tokens = input_tokens + output_tokens
            await self.token_limiter.reconcile(
                reservation_id=ctx.reservation_id,
                actual_tokens=total_tokens,
                actual_usd=0.0,  # USD cost would come from model pricing
            )
            logger.debug(
                "MeterStage: reconciled reservation %s with %d input + %d output tokens",
                ctx.reservation_id,
                input_tokens,
                output_tokens,
            )

        return ctx
