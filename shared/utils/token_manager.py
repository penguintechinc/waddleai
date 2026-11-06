"""WaddleAI Dual Token System.

Manages both WaddleAI tokens (normalized) and raw LLM tokens with conversion rates.
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

import tiktoken

from shared.quota.virtual_price import (
    LocalDeployment,
    LocalRateMissingError,
    resolve_local_deployment,
    snapshot_price,
    usage_row_key,
)
from shared.utils.default_conversion_rates import DEFAULT_RATE_ROWS

logger = logging.getLogger(__name__)

_DEFAULT_RATE_REFRESH_SECONDS = 60.0


def _rate_refresh_seconds() -> float:
    """Refresh interval from WADDLEAI_RATE_REFRESH_SECONDS; bad/non-positive values -> default."""
    raw = os.environ.get("WADDLEAI_RATE_REFRESH_SECONDS")
    try:
        value = float(raw) if raw else _DEFAULT_RATE_REFRESH_SECONDS
    except ValueError:
        return _DEFAULT_RATE_REFRESH_SECONDS
    return value if value > 0 else _DEFAULT_RATE_REFRESH_SECONDS


class TokenType(Enum):
    """Types of tokens."""

    WADDLEAI = "waddleai"
    LLM_INPUT = "llm_input"
    LLM_OUTPUT = "llm_output"


@dataclass
class TokenUsage:
    """Token usage record."""

    waddleai_tokens: int
    llm_tokens_input: int
    llm_tokens_output: int
    llm_tokens_breakdown: dict[str, dict[str, int]]  # {"model": {"input": x, "output": y}}
    cost_estimate_waddleai: float
    cost_estimate_usd: float


@dataclass
class ConversionRate:
    """Token conversion rate configuration."""

    provider: str
    model: str
    input_rate: float  # LLM tokens per WaddleAI token
    output_rate: float
    base_cost_per_waddleai_token: float


class TokenManager:
    """Manages token counting, conversion, and quota enforcement."""

    # Fallback when token_conversion_rates is empty -- derived from the ONE canonical seed.
    DEFAULT_CONVERSION_RATES = {
        f"{r.provider}:{r.model}": ConversionRate(
            r.provider, r.model, r.input_rate, r.output_rate, r.base_cost_per_waddleai_token
        )
        for r in DEFAULT_RATE_ROWS
    }

    def __init__(self, db):
        """Bind the DAL handle, load conversion rates, and build per-provider token encoders."""
        self.db = db
        self._rate_refresh_s = _rate_refresh_seconds()
        self._load_conversion_rates()

        # Initialize token encoders for different providers
        self.encoders = {
            "openai": {
                "gpt-4": tiktoken.encoding_for_model("gpt-4"),
                "gpt-3.5-turbo": tiktoken.encoding_for_model("gpt-3.5-turbo"),
            }
        }

        # Default fallback encoder
        self.default_encoder = tiktoken.encoding_for_model("gpt-3.5-turbo")

    def _load_conversion_rates(self):
        """Load token conversion rates from database, fallback to defaults if empty."""
        new_rates: dict[str, ConversionRate] = {}

        rates = self.db(self.db.token_conversion_rates.enabled == True).select()  # noqa: E712
        if rates:
            for rate in rates:
                key = f"{rate.provider}:{rate.model}"
                new_rates[key] = ConversionRate(
                    provider=rate.provider,
                    model=rate.model,
                    input_rate=rate.input_rate,
                    output_rate=rate.output_rate,
                    base_cost_per_waddleai_token=rate.base_cost_per_waddleai_token,
                )
        else:
            # Fallback to defaults when DB table is empty
            new_rates = dict(self.DEFAULT_CONVERSION_RATES)
        # Single atomic swap: the live dict is never observed empty or half-filled.
        self.conversion_rates = new_rates
        self._rates_loaded_at = time.monotonic()

    def rates_stale(self, now: float | None = None) -> bool:
        """True when the loaded conversion rates are older than the refresh interval."""
        current = time.monotonic() if now is None else now
        return current - self._rates_loaded_at >= self._rate_refresh_s

    def refresh_rates_if_stale(self, now: float | None = None) -> bool:
        """Reload rates from the DB when stale; keep the old rates on failure (never raises)."""
        if not self.rates_stale(now):
            return False
        try:
            self._load_conversion_rates()
        except Exception as exc:  # noqa: BLE001 -- a DB blip must not break billing
            logger.warning("conversion-rate refresh failed; keeping previous rates: %s", exc)
            self._rates_loaded_at = time.monotonic()  # back off one interval
            return False
        return True

    @staticmethod
    def _credits_for(rate: "ConversionRate", input_tokens: int, output_tokens: int) -> int:
        """WaddleAI tokens for one rate: ``max(1, in/input_rate) + max(1, out/output_rate)``."""
        waddleai_input = max(1, int(input_tokens / rate.input_rate)) if input_tokens > 0 else 0
        waddleai_output = max(1, int(output_tokens / rate.output_rate)) if output_tokens > 0 else 0
        return waddleai_input + waddleai_output

    def providers_for_model(self, model: str) -> frozenset[str]:
        """Providers with a loaded rate for ``model`` (empty when the model is unknown)."""
        return frozenset(r.provider for r in self.conversion_rates.values() if r.model == model)

    def estimate_credits(self, model: str, input_tokens: int, output_tokens: int) -> int:
        """Conservative credit estimate when the provider is unknown (reserve time, spec §7).

        Takes the maximum over every provider's rate for ``model``; an unknown model uses the
        default formula. Always at least 1 so a reservation can never be free.
        """
        candidates = [r for r in self.conversion_rates.values() if r.model == model]
        if not candidates:
            return max(1, (input_tokens + output_tokens * 2) // 10)
        return max(1, max(self._credits_for(r, input_tokens, output_tokens) for r in candidates))

    def count_tokens(
        self, text: str, provider: str = "openai", model: str = "gpt-3.5-turbo"
    ) -> int:
        """Count tokens in text using appropriate encoder."""
        try:
            # Use provider-specific encoder if available
            if provider in self.encoders and model in self.encoders[provider]:
                encoder = self.encoders[provider][model]
            else:
                encoder = self.default_encoder

            return len(encoder.encode(text))
        except Exception as e:
            logger.warning(f"Token counting failed for {provider}:{model}, using fallback: {e}")
            # Fallback: rough estimation (4 chars = 1 token)
            return len(text) // 4

    def calculate_waddleai_tokens(
        self, input_tokens: int, output_tokens: int, provider: str, model: str
    ) -> int:
        """Convert LLM tokens to WaddleAI tokens using conversion rates."""
        rate_key = f"{provider}:{model}"

        if rate_key not in self.conversion_rates:
            logger.warning(f"No conversion rate found for {provider}:{model}, using default")
            # Default conversion rate: output tokens weighted 2x, divided by 10
            return max(1, (input_tokens + output_tokens * 2) // 10)

        rate = self.conversion_rates[rate_key]

        return self._credits_for(rate, input_tokens, output_tokens)

    def calculate_cost(
        self, waddleai_tokens: int, provider: str, model: str
    ) -> tuple[float, float]:
        """Calculate cost in WaddleAI tokens and USD."""
        rate_key = f"{provider}:{model}"

        cost_waddleai = float(waddleai_tokens)  # 1:1 for WaddleAI tokens

        if rate_key in self.conversion_rates:
            rate = self.conversion_rates[rate_key]
            cost_usd = float(waddleai_tokens * rate.base_cost_per_waddleai_token)
        else:
            cost_usd = float(waddleai_tokens * 0.001)  # Default $0.001 per WaddleAI token

        return cost_waddleai, cost_usd

    def process_usage(
        self,
        input_text: str,
        output_text: str,
        provider: str,
        model: str,
        api_key_id: int,
        user_id: int,
        organization_id: int,
        actual_input_tokens: int | None = None,
        actual_output_tokens: int | None = None,
        estimated: bool = False,
        local_deployment_kind: int | None = None,
        local_deployment_id: int | None = None,
        unit_price_input: Decimal | None = None,
        unit_price_output: Decimal | None = None,
    ) -> TokenUsage:
        """Process token usage for a request.

        actual_input_tokens/actual_output_tokens: exact counts reported by the
        provider/connector (or the upstream stub), when available. Both proxy
        call sites (chat_completions, claude_messages) always pass these, so
        this falls back to local tiktoken estimation only when a caller omits
        them.
        """
        self.refresh_rates_if_stale()
        local: LocalDeployment | None = None
        if local_deployment_kind is not None or local_deployment_id is not None:
            # Locally hosted: charge at THIS deployment's virtual rate (AM26/AM27) and snapshot
            # the price in force. A missing rate is refused, never the default rate.
            local = self.require_local_rate(local_deployment_kind, local_deployment_id)
            provider, model = local.provider, local.model_key
            # The connector-stamped unit_price_* arguments are advisory only; the deployment row
            # (local.price_*) is authoritative for the snapshot.
        # Count LLM tokens: prefer the provider-reported counts when given,
        # else fall back to local estimation.
        input_tokens = (
            actual_input_tokens
            if actual_input_tokens is not None
            else self.count_tokens(input_text, provider, model)
        )
        output_tokens = (
            actual_output_tokens
            if actual_output_tokens is not None
            else self.count_tokens(output_text, provider, model)
        )

        # Counts are untrusted engine/connector output: a negative never reduces a debit.
        input_tokens = max(0, input_tokens)
        output_tokens = max(0, output_tokens)

        # Convert to WaddleAI tokens
        waddleai_tokens = self.calculate_waddleai_tokens(
            input_tokens, output_tokens, provider, model
        )

        # Calculate costs
        cost_waddleai, cost_usd = self.calculate_cost(waddleai_tokens, provider, model)

        # Create breakdown
        model_key = f"{provider}_{model.replace('-', '_')}"
        llm_breakdown = {model_key: {"input": input_tokens, "output": output_tokens}}

        usage = TokenUsage(
            waddleai_tokens=waddleai_tokens,
            llm_tokens_input=input_tokens,
            llm_tokens_output=output_tokens,
            llm_tokens_breakdown=llm_breakdown,
            cost_estimate_waddleai=cost_waddleai,
            cost_estimate_usd=cost_usd,
        )

        # Update database
        self._update_usage_records(
            usage,
            api_key_id,
            user_id,
            organization_id,
            provider,
            model,
            estimated=estimated,
            local=local,
        )

        return usage

    def require_local_rate(self, kind: int | None, deployment_id: int | None) -> LocalDeployment:
        """Resolve a local deployment and its rate row, or refuse (fail closed, AM27).

        Reloads the rate table once on a miss (a rate written moments ago may not be loaded
        yet); still missing -> ``waddleai_local_rate_missing_total`` +1 and
        :class:`LocalRateMissingError`. The default conversion rate is never applied.
        """
        local = resolve_local_deployment(self.db, kind, deployment_id) if kind is not None else None
        if local is not None:
            key = f"{local.provider}:{local.model_key}"
            if key not in self.conversion_rates:
                try:
                    self._load_conversion_rates()
                except Exception as exc:  # noqa: BLE001 -- a DB blip must not become a default
                    logger.warning("rate reload for local deployment failed: %s", exc)
            if key in self.conversion_rates:
                return local
        self._count_local_rate_missing(kind)
        raise LocalRateMissingError(
            f"no virtual rate for local deployment kind={kind} id={deployment_id}; request refused"
        )

    def local_rate_present(self, kind: int | None, deployment_id: int | None) -> bool:
        """True when the deployment resolves and its rate exists (pre-dispatch fail-closed gate)."""
        try:
            self.require_local_rate(kind, deployment_id)
        except LocalRateMissingError:
            return False
        return True

    @staticmethod
    def _count_local_rate_missing(kind: int | None) -> None:
        """Bump ``waddleai_local_rate_missing_total``; telemetry never breaks billing."""
        try:
            from shared.observability.vllm_metrics import local_rate_missing_counter

            local_rate_missing_counter().add(1, {"kind": str(kind)})
        except Exception:  # noqa: BLE001
            logger.debug("local-rate-missing counter emission failed", exc_info=True)

    def _update_usage_records(
        self,
        usage: TokenUsage,
        api_key_id: int,
        user_id: int,
        organization_id: int,
        provider: str,
        model: str,
        estimated: bool = False,
        local: LocalDeployment | None = None,
    ):
        """Update usage records in database.

        ``token_usage`` is a per-(api key, day) rollup. Locally hosted usage adds the snapshot
        key (kind, deployment, unit prices) so a price or deployment change opens a NEW row and
        earlier rows keep the price they were charged at; commercial usage only ever merges
        into rows with no local kind.
        """
        today = date.today()
        usage_table = self.db.token_usage
        condition = (usage_table.api_key_id == api_key_id) & (usage_table.date == today)
        has_snapshot_cols = hasattr(usage_table, "local_deployment_kind")
        if has_snapshot_cols:
            if local is None:
                condition = condition & (usage_table.local_deployment_kind == None)  # noqa: E711
            else:
                # Prices are matched in Python (below): SQL equality on a NUMERIC column is
                # driver/dialect dependent, and only a handful of rows share a deployment/day.
                condition = (
                    condition
                    & (usage_table.local_deployment_kind == local.kind)
                    & (usage_table.local_deployment_id == local.deployment_id)
                )
        elif local is not None:
            raise RuntimeError("token_usage has no chargeback snapshot columns (migration 028)")

        # Update daily usage record
        existing = self.db(condition).select().first()
        if local is not None and has_snapshot_cols:
            want = usage_row_key(
                local.kind, local.deployment_id, local.price_input, local.price_output
            )
            existing = next(
                (
                    r
                    for r in self.db(condition).select()
                    if usage_row_key(
                        r.local_deployment_kind,
                        r.local_deployment_id,
                        r.unit_price_input,
                        r.unit_price_output,
                    )
                    == want
                ),
                None,
            )

        if existing:
            # Update existing record
            new_waddleai = existing.waddleai_tokens + usage.waddleai_tokens
            new_input = existing.tokens_input_total + usage.llm_tokens_input
            new_output = existing.tokens_output_total + usage.llm_tokens_output

            # Merge LLM token breakdowns
            existing_breakdown = json.loads(existing.llm_tokens) if existing.llm_tokens else {}
            for model_key, tokens in usage.llm_tokens_breakdown.items():
                if model_key in existing_breakdown:
                    existing_breakdown[model_key]["input"] += tokens["input"]
                    existing_breakdown[model_key]["output"] += tokens["output"]
                else:
                    existing_breakdown[model_key] = tokens

            # regression: bug found writing tests/e2e/test_response_cache_e2e.py --
            # penguin_dal's Row (penguin_dal/query.py) has no update_record()
            # method (that's classic PyDAL API); the correct penguin_dal
            # update is db(condition).update(**kwargs). This previously
            # raised AttributeError on the *second* request in the same day
            # for the same api_key, caught by the route handler's broad
            # except and reported as a 500 -- token usage silently stopped
            # accumulating past the first request/key/day in production.
            self.db(self.db.token_usage.id == existing.id).update(
                waddleai_tokens=new_waddleai,
                tokens_input_total=new_input,
                tokens_output_total=new_output,
                llm_tokens=json.dumps(existing_breakdown),
                request_count=existing.request_count + 1,
                last_updated=datetime.utcnow(),
                **(
                    {"estimated": bool(getattr(existing, "estimated", False)) or estimated}
                    if has_snapshot_cols
                    else {}
                ),
            )
        else:
            # Create new record
            snapshot: dict[str, Any] = {}
            if has_snapshot_cols:
                snapshot = {"estimated": estimated}
                if local is not None:
                    snapshot.update(
                        local_deployment_kind=local.kind,
                        local_deployment_id=local.deployment_id,
                        unit_price_input=str(snapshot_price(local.price_input)),
                        unit_price_output=str(snapshot_price(local.price_output)),
                    )
            self.db.token_usage.insert(
                api_key_id=api_key_id,
                user_id=user_id,
                organization_id=organization_id,
                date=today,
                waddleai_tokens=usage.waddleai_tokens,
                tokens_input_total=usage.llm_tokens_input,
                tokens_output_total=usage.llm_tokens_output,
                llm_tokens=json.dumps(usage.llm_tokens_breakdown),
                request_count=1,
                **snapshot,
            )

        # Update cache for quota checking
        self._update_usage_cache(usage, api_key_id, organization_id)

    def _update_usage_cache(self, usage: TokenUsage, api_key_id: int, organization_id: int):
        """Update real-time usage cache for quota enforcement."""
        now = datetime.utcnow()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = today.replace(day=1)

        # Update daily cache
        daily_cache = (
            self.db(
                (self.db.usage_cache.api_key_id == api_key_id)
                & (self.db.usage_cache.period == "daily")
                & (self.db.usage_cache.period_start == today)
            )
            .select()
            .first()
        )

        if daily_cache:
            new_waddleai = daily_cache.waddleai_tokens_used + usage.waddleai_tokens

            # Update LLM token breakdown
            llm_tokens = (
                json.loads(daily_cache.llm_tokens_used) if daily_cache.llm_tokens_used else {}
            )
            for model_key, tokens in usage.llm_tokens_breakdown.items():
                if model_key in llm_tokens:
                    llm_tokens[model_key]["input"] += tokens["input"]
                    llm_tokens[model_key]["output"] += tokens["output"]
                else:
                    llm_tokens[model_key] = tokens

            # regression: see the identical fix above -- update_record() is
            # not a penguin_dal Row method.
            self.db(self.db.usage_cache.id == daily_cache.id).update(
                waddleai_tokens_used=new_waddleai,
                llm_tokens_used=json.dumps(llm_tokens),
                requests_made=daily_cache.requests_made + 1,
                last_updated=now,
            )
        else:
            self.db.usage_cache.insert(
                api_key_id=api_key_id,
                organization_id=organization_id,
                period="daily",
                period_start=today,
                waddleai_tokens_used=usage.waddleai_tokens,
                llm_tokens_used=json.dumps(usage.llm_tokens_breakdown),
                requests_made=1,
            )

        # Update monthly cache
        monthly_cache = (
            self.db(
                (self.db.usage_cache.api_key_id == api_key_id)
                & (self.db.usage_cache.period == "monthly")
                & (self.db.usage_cache.period_start == month_start)
            )
            .select()
            .first()
        )

        if monthly_cache:
            new_waddleai = monthly_cache.waddleai_tokens_used + usage.waddleai_tokens

            # Update LLM token breakdown
            llm_tokens = (
                json.loads(monthly_cache.llm_tokens_used) if monthly_cache.llm_tokens_used else {}
            )
            for model_key, tokens in usage.llm_tokens_breakdown.items():
                if model_key in llm_tokens:
                    llm_tokens[model_key]["input"] += tokens["input"]
                    llm_tokens[model_key]["output"] += tokens["output"]
                else:
                    llm_tokens[model_key] = tokens

            # regression: see the identical fix above -- update_record() is
            # not a penguin_dal Row method.
            self.db(self.db.usage_cache.id == monthly_cache.id).update(
                waddleai_tokens_used=new_waddleai,
                llm_tokens_used=json.dumps(llm_tokens),
                requests_made=monthly_cache.requests_made + 1,
                last_updated=now,
            )
        else:
            self.db.usage_cache.insert(
                api_key_id=api_key_id,
                organization_id=organization_id,
                period="monthly",
                period_start=month_start,
                waddleai_tokens_used=usage.waddleai_tokens,
                llm_tokens_used=json.dumps(usage.llm_tokens_breakdown),
                requests_made=1,
            )

    def check_quota(self, api_key_id: int) -> tuple[bool, dict[str, Any]]:
        """Check if API key is within quota limits."""
        # Get API key and user info
        api_key = self.db(self.db.api_keys.id == api_key_id).select().first()
        if not api_key:
            return False, {"error": "API key not found"}

        user = self.db(self.db.users.id == api_key.user_id).select().first()
        if not user:
            return False, {"error": "User not found"}

        # Determine quota limits (API key overrides user)
        daily_limit = api_key.token_quota_daily or user.token_quota_daily
        monthly_limit = api_key.token_quota_monthly or user.token_quota_monthly

        # Get current usage
        now = datetime.utcnow()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = today.replace(day=1)

        daily_usage = (
            self.db(
                (self.db.usage_cache.api_key_id == api_key_id)
                & (self.db.usage_cache.period == "daily")
                & (self.db.usage_cache.period_start == today)
            )
            .select()
            .first()
        )

        monthly_usage = (
            self.db(
                (self.db.usage_cache.api_key_id == api_key_id)
                & (self.db.usage_cache.period == "monthly")
                & (self.db.usage_cache.period_start == month_start)
            )
            .select()
            .first()
        )

        daily_used = daily_usage.waddleai_tokens_used if daily_usage else 0
        monthly_used = monthly_usage.waddleai_tokens_used if monthly_usage else 0

        # Check limits
        daily_ok = daily_used < daily_limit
        monthly_ok = monthly_used < monthly_limit

        quota_info = {
            "daily": {
                "used": daily_used,
                "limit": daily_limit,
                "remaining": daily_limit - daily_used,
                "ok": daily_ok,
            },
            "monthly": {
                "used": monthly_used,
                "limit": monthly_limit,
                "remaining": monthly_limit - monthly_used,
                "ok": monthly_ok,
            },
        }

        return daily_ok and monthly_ok, quota_info

    def get_usage_stats(
        self,
        api_key_id: int | None = None,
        user_id: int | None = None,
        organization_id: int | None = None,
        days: int = 30,
    ) -> dict[str, Any]:
        """Get detailed usage statistics."""
        since = datetime.utcnow().date() - timedelta(days=days)

        conditions = [self.db.token_usage.date >= since]

        if api_key_id:
            conditions.append(self.db.token_usage.api_key_id == api_key_id)
        elif user_id:
            conditions.append(self.db.token_usage.user_id == user_id)
        elif organization_id:
            conditions.append(self.db.token_usage.organization_id == organization_id)

        query = conditions[0]
        for condition in conditions[1:]:
            query = query & condition

        usage_records = self.db(query).select()

        stats: dict[str, Any] = {
            "total_waddleai_tokens": 0,
            "total_llm_input_tokens": 0,
            "total_llm_output_tokens": 0,
            "total_requests": 0,
            "llm_breakdown": {},
            "daily_usage": {},
            "average_daily": 0,
        }

        for record in usage_records:
            stats["total_waddleai_tokens"] += record.waddleai_tokens
            stats["total_llm_input_tokens"] += record.tokens_input_total
            stats["total_llm_output_tokens"] += record.tokens_output_total
            stats["total_requests"] += record.request_count

            # Daily breakdown
            date_str = record.date.strftime("%Y-%m-%d")
            stats["daily_usage"][date_str] = {
                "waddleai_tokens": record.waddleai_tokens,
                "llm_input": record.tokens_input_total,
                "llm_output": record.tokens_output_total,
                "requests": record.request_count,
            }

            # LLM model breakdown
            if record.llm_tokens:
                llm_data = json.loads(record.llm_tokens)
                for model, tokens in llm_data.items():
                    if model not in stats["llm_breakdown"]:
                        stats["llm_breakdown"][model] = {"input": 0, "output": 0}
                    stats["llm_breakdown"][model]["input"] += tokens.get("input", 0)
                    stats["llm_breakdown"][model]["output"] += tokens.get("output", 0)

        # Calculate averages
        if len(usage_records) > 0:
            stats["average_daily"] = stats["total_waddleai_tokens"] // max(1, days)

        return stats


def create_token_manager(db) -> TokenManager:
    """Factory function to create token manager."""
    return TokenManager(db)
