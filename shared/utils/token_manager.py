"""
WaddleAI Dual Token System
Manages both WaddleAI tokens (normalized) and raw LLM tokens with conversion rates
"""

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Dict, Optional, Tuple

import tiktoken

logger = logging.getLogger(__name__)


class TokenType(Enum):
    """Types of tokens"""

    WADDLEAI = "waddleai"
    LLM_INPUT = "llm_input"
    LLM_OUTPUT = "llm_output"


@dataclass
class TokenUsage:
    """Token usage record"""

    waddleai_tokens: int
    llm_tokens_input: int
    llm_tokens_output: int
    llm_tokens_breakdown: Dict[str, Dict[str, int]]  # {"model": {"input": x, "output": y}}
    cost_estimate_waddleai: float
    cost_estimate_usd: float


@dataclass
class ConversionRate:
    """Token conversion rate configuration"""

    provider: str
    model: str
    input_rate: float  # LLM tokens per WaddleAI token
    output_rate: float
    base_cost_per_waddleai_token: float


class TokenManager:
    """Manages token counting, conversion, and quota enforcement"""

    # Default conversion rates for common models (seed data for token_conversion_rates table)
    DEFAULT_CONVERSION_RATES = {
        "openai:gpt-4": ConversionRate("openai", "gpt-4", 10.0, 20.0, 0.003),
        "openai:gpt-4-turbo": ConversionRate("openai", "gpt-4-turbo", 10.0, 20.0, 0.001),
        "openai:gpt-4o": ConversionRate("openai", "gpt-4o", 10.0, 20.0, 0.0005),
        "openai:gpt-4o-mini": ConversionRate("openai", "gpt-4o-mini", 10.0, 10.0, 0.00015),
        "openai:gpt-3.5-turbo": ConversionRate("openai", "gpt-3.5-turbo", 10.0, 10.0, 0.0002),
        "anthropic:claude-3-opus": ConversionRate("anthropic", "claude-3-opus", 10.0, 20.0, 0.0075),
        "anthropic:claude-3-sonnet": ConversionRate("anthropic", "claude-3-sonnet", 10.0, 20.0, 0.0015),
        "anthropic:claude-3-haiku": ConversionRate("anthropic", "claude-3-haiku", 10.0, 10.0, 0.00025),
        "anthropic:claude-3.5-sonnet": ConversionRate("anthropic", "claude-3.5-sonnet", 10.0, 20.0, 0.003),
        "ollama:llama2": ConversionRate("ollama", "llama2", 10.0, 10.0, 0.0),
        "ollama:mistral": ConversionRate("ollama", "mistral", 10.0, 10.0, 0.0),
        "ollama:codellama": ConversionRate("ollama", "codellama", 10.0, 10.0, 0.0),
    }

    def __init__(self, db):
        self.db = db
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
        """Load token conversion rates from database, fallback to defaults if empty"""
        self.conversion_rates = {}

        rates = self.db(self.db.token_conversion_rates.enabled == True).select()  # noqa: E712
        if rates:
            for rate in rates:
                key = f"{rate.provider}:{rate.model}"
                self.conversion_rates[key] = ConversionRate(
                    provider=rate.provider,
                    model=rate.model,
                    input_rate=rate.input_rate,
                    output_rate=rate.output_rate,
                    base_cost_per_waddleai_token=rate.base_cost_per_waddleai_token,
                )
        else:
            # Fallback to defaults when DB table is empty
            self.conversion_rates = dict(self.DEFAULT_CONVERSION_RATES)

    def count_tokens(self, text: str, provider: str = "openai", model: str = "gpt-3.5-turbo") -> int:
        """Count tokens in text using appropriate encoder"""
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

    def calculate_waddleai_tokens(self, input_tokens: int, output_tokens: int, provider: str, model: str) -> int:
        """Convert LLM tokens to WaddleAI tokens using conversion rates"""
        rate_key = f"{provider}:{model}"

        if rate_key not in self.conversion_rates:
            logger.warning(f"No conversion rate found for {provider}:{model}, using default")
            # Default conversion rate: output tokens weighted 2x, divided by 10
            return max(1, (input_tokens + output_tokens * 2) // 10)

        rate = self.conversion_rates[rate_key]

        # Convert using rates (LLM tokens per WaddleAI token)
        waddleai_input = max(1, int(input_tokens / rate.input_rate)) if input_tokens > 0 else 0
        waddleai_output = max(1, int(output_tokens / rate.output_rate)) if output_tokens > 0 else 0

        return waddleai_input + waddleai_output

    def calculate_cost(self, waddleai_tokens: int, provider: str, model: str) -> Tuple[float, float]:
        """Calculate cost in WaddleAI tokens and USD"""
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
        actual_input_tokens: Optional[int] = None,
        actual_output_tokens: Optional[int] = None,
    ) -> TokenUsage:
        """Process token usage for a request.

        actual_input_tokens/actual_output_tokens: exact counts reported by the
        provider/connector (or the upstream stub), when available. Both proxy
        call sites (chat_completions, claude_messages) always pass these, so
        this falls back to local tiktoken estimation only when a caller omits
        them.
        """

        # Count LLM tokens: prefer the provider-reported counts when given,
        # else fall back to local estimation.
        input_tokens = (
            actual_input_tokens if actual_input_tokens is not None else self.count_tokens(input_text, provider, model)
        )
        output_tokens = (
            actual_output_tokens
            if actual_output_tokens is not None
            else self.count_tokens(output_text, provider, model)
        )

        # Convert to WaddleAI tokens
        waddleai_tokens = self.calculate_waddleai_tokens(input_tokens, output_tokens, provider, model)

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
        self._update_usage_records(usage, api_key_id, user_id, organization_id, provider, model)

        return usage

    def _update_usage_records(
        self, usage: TokenUsage, api_key_id: int, user_id: int, organization_id: int, provider: str, model: str
    ):
        """Update usage records in database"""
        today = date.today()

        # Update daily usage record
        existing = (
            self.db((self.db.token_usage.api_key_id == api_key_id) & (self.db.token_usage.date == today))
            .select()
            .first()
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
            )
        else:
            # Create new record
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
            )

        # Update cache for quota checking
        self._update_usage_cache(usage, api_key_id, organization_id)

    def _update_usage_cache(self, usage: TokenUsage, api_key_id: int, organization_id: int):
        """Update real-time usage cache for quota enforcement"""
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
            llm_tokens = json.loads(daily_cache.llm_tokens_used) if daily_cache.llm_tokens_used else {}
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
            llm_tokens = json.loads(monthly_cache.llm_tokens_used) if monthly_cache.llm_tokens_used else {}
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

    def check_quota(self, api_key_id: int) -> Tuple[bool, Dict[str, Any]]:
        """Check if API key is within quota limits"""
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
            "daily": {"used": daily_used, "limit": daily_limit, "remaining": daily_limit - daily_used, "ok": daily_ok},
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
        api_key_id: Optional[int] = None,
        user_id: Optional[int] = None,
        organization_id: Optional[int] = None,
        days: int = 30,
    ) -> Dict[str, Any]:
        """Get detailed usage statistics"""

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

        stats = {
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
    """Factory function to create token manager"""
    return TokenManager(db)
