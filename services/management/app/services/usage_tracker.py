"""
WaddleAI Usage Tracking Service

Records usage events directly into `token_usage` (the AILB webhook ingest
path and its raw per-event AILB bookkeeping table were retired alongside
migration 007 -- there is no successor raw-event log, only the
`token_usage` aggregate this module already maintained).
Provides LiteLLM-style usage tracking, quotas, and billing.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class QuotaStatus(str, Enum):
    """Quota status"""

    OK = "ok"
    WARNING = "warning"  # >80% used
    EXCEEDED = "exceeded"
    DISABLED = "disabled"


@dataclass
class UsageEvent:
    """A single completed-request usage event to fold into token_usage."""

    event_id: str
    key_id: str
    request_id: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    status: str
    error_message: Optional[str] = None
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow()


@dataclass
class DailyUsage:
    """Daily usage summary"""

    date: date
    key_id: Optional[int] = None
    user_id: Optional[int] = None
    organization_id: Optional[int] = None
    waddleai_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    request_count: int = 0
    cost_usd: float = 0.0


@dataclass
class UsageStats:
    """Usage statistics"""

    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    request_count: int = 0
    cost_usd: float = 0.0
    by_model: Dict[str, int] = field(default_factory=dict)
    by_provider: Dict[str, int] = field(default_factory=dict)
    by_day: Dict[str, int] = field(default_factory=dict)


@dataclass
class QuotaInfo:
    """Quota information"""

    status: QuotaStatus
    limit: Optional[int] = None
    used: int = 0
    remaining: Optional[int] = None
    percentage: float = 0.0
    resets_at: Optional[datetime] = None


class UsageTrackingService:
    """
    Tracks usage events and enforces quotas.

    Features:
    - Record usage events directly into token_usage
    - Convert LLM tokens to WaddleAI normalized tokens
    - Aggregate usage by day, user, organization, key
    - Quota checking and enforcement
    """

    def __init__(self, db, redis_client=None):
        self.db = db
        self.redis = redis_client
        self._conversion_rates_cache = {}
        self._cache_ttl = 300  # 5 minutes

    def record_usage(self, event: UsageEvent) -> bool:
        """
        Record a usage event by updating the `token_usage` aggregate.

        Args:
            event: UsageEvent to record

        Returns:
            True if recorded successfully. False if event.key_id does not
            resolve to a known virtual key -- there is no raw-event fallback
            table to fall back to, so an unresolvable key means nothing is
            persisted (callers should treat this as a signal to investigate,
            not silently retry).
        """
        db = self.db

        # Find virtual key by its public prefix (the only stable lookup
        # left once the AILB-specific key-id column was dropped).
        virtual_key = None
        if event.key_id and event.key_id.startswith("wa-"):
            virtual_key = db(db.virtual_keys.key_prefix.like(f"{event.key_id[:12]}%")).select().first()

        if not virtual_key:
            logger.warning("record_usage: no key for key_id=%s, dropping event", event.key_id)
            return False

        # Calculate WaddleAI tokens
        waddleai_tokens = self.calculate_waddleai_tokens(
            event.provider, event.model, event.input_tokens, event.output_tokens
        )

        self._update_usage_aggregates(virtual_key, event, waddleai_tokens)

        db.commit()
        return True

    def calculate_waddleai_tokens(self, provider: str, model: str, input_tokens: int, output_tokens: int) -> int:
        """
        Calculate WaddleAI normalized tokens from LLM tokens.

        Uses conversion rates from database, falls back to defaults.
        """
        db = self.db

        # Check cache
        cache_key = f"{provider}:{model}"
        if cache_key in self._conversion_rates_cache:
            cached = self._conversion_rates_cache[cache_key]
            if cached["expires"] > datetime.utcnow():
                input_rate = cached["input_rate"]
                output_rate = cached["output_rate"]
                return int(input_tokens / input_rate) + int(output_tokens / output_rate)

        # Look up conversion rate
        rate = (
            db(
                (db.token_conversion_rates.provider == provider)
                & (db.token_conversion_rates.model == model)
                & (db.token_conversion_rates.enabled is True)
            )
            .select()
            .first()
        )

        if rate:
            input_rate = rate.input_rate or 10
            output_rate = rate.output_rate or 10
        else:
            # Default rates based on provider
            input_rate, output_rate = self._get_default_rates(provider, model)

        # Cache the rates
        self._conversion_rates_cache[cache_key] = {
            "input_rate": input_rate,
            "output_rate": output_rate,
            "expires": datetime.utcnow() + timedelta(seconds=self._cache_ttl),
        }

        return int(input_tokens / input_rate) + int(output_tokens / output_rate)

    def _get_default_rates(self, provider: str, model: str) -> Tuple[int, int]:
        """Get default conversion rates by provider/model"""
        # More expensive models have lower rates (more WaddleAI tokens per LLM token)
        # Cheaper models have higher rates

        if provider == "openai":
            if model.startswith("gpt-4"):
                return (5, 5)  # 1 WaddleAI token = 5 GPT-4 tokens
            elif model.startswith("o1"):
                return (3, 3)  # o1 is more expensive
            else:
                return (15, 15)  # GPT-3.5 is cheaper

        elif provider == "anthropic":
            if "opus" in model:
                return (3, 3)  # Opus is expensive
            elif "sonnet" in model:
                return (8, 8)  # Sonnet is mid-range
            else:
                return (20, 20)  # Haiku is cheap

        elif provider == "gemini":
            if "pro" in model:
                return (8, 8)
            else:
                return (15, 15)

        elif provider == "ollama":
            return (100, 100)  # Local models are effectively free

        elif provider == "bedrock":
            return (5, 5)  # Default to GPT-4 equivalent

        elif provider == "azure_openai":
            return (5, 5)

        elif provider == "cohere":
            return (10, 10)

        # Default fallback
        return (10, 10)

    def _update_usage_aggregates(self, virtual_key, event: UsageEvent, waddleai_tokens: int):
        """Update aggregated usage tables"""
        db = self.db
        today = date.today()

        # Get or create daily usage record
        usage_record = (
            db((db.token_usage.virtual_key_id == virtual_key.id) & (db.token_usage.date == today)).select().first()
        )

        if usage_record:
            db(db.token_usage.id == usage_record.id).update(
                waddleai_tokens=(usage_record.waddleai_tokens or 0) + waddleai_tokens,
                tokens_input_total=(usage_record.tokens_input_total or 0) + event.input_tokens,
                tokens_output_total=(usage_record.tokens_output_total or 0) + event.output_tokens,
                request_count=(usage_record.request_count or 0) + 1,
                cost_usd_total=(usage_record.cost_usd_total or 0) + event.cost_usd,
                last_updated=datetime.utcnow(),
            )
        else:
            db.token_usage.insert(
                virtual_key_id=virtual_key.id,
                user_id=virtual_key.user_id,
                organization_id=virtual_key.organization_id,
                date=today,
                waddleai_tokens=waddleai_tokens,
                tokens_input_total=event.input_tokens,
                tokens_output_total=event.output_tokens,
                request_count=1,
                cost_usd_total=event.cost_usd,
                last_updated=datetime.utcnow(),
            )

        # Update key last_used
        db(db.virtual_keys.id == virtual_key.id).update(last_used=datetime.utcnow())

        # Create detailed usage log
        db.usage_logs.insert(
            timestamp=datetime.utcnow(),
            virtual_key_id=virtual_key.id,
            user_id=virtual_key.user_id,
            organization_id=virtual_key.organization_id,
            request_hash=event.request_id,
            waddleai_tokens_used=waddleai_tokens,
            llm_tokens_input=event.input_tokens,
            llm_tokens_output=event.output_tokens,
            llm_tokens_total=event.input_tokens + event.output_tokens,
            response_time=event.latency_ms / 1000.0 if event.latency_ms else None,
            status_code=200 if event.status == "success" else 500,
            model_used=event.model,
            provider_type=event.provider,
            cost_estimate_waddleai=waddleai_tokens * 0.001,
            cost_estimate_usd=event.cost_usd,
        )

    def aggregate_daily_usage(self, key_id: int, target_date: date) -> DailyUsage:
        """Get aggregated daily usage for a key"""
        db = self.db

        usage = db((db.token_usage.virtual_key_id == key_id) & (db.token_usage.date == target_date)).select().first()

        if usage:
            return DailyUsage(
                date=target_date,
                key_id=key_id,
                user_id=usage.user_id,
                organization_id=usage.organization_id,
                waddleai_tokens=usage.waddleai_tokens or 0,
                input_tokens=usage.tokens_input_total or 0,
                output_tokens=usage.tokens_output_total or 0,
                request_count=usage.request_count or 0,
                cost_usd=usage.cost_usd_total or 0,
            )

        return DailyUsage(date=target_date, key_id=key_id)

    def get_usage_stats(
        self,
        key_id: Optional[int] = None,
        user_id: Optional[int] = None,
        organization_id: Optional[int] = None,
        days: int = 30,
    ) -> UsageStats:
        """Get usage statistics"""
        db = self.db
        start_date = date.today() - timedelta(days=days)

        # Build query
        query = db.token_usage.date >= start_date
        if key_id:
            query &= db.token_usage.virtual_key_id == key_id
        elif user_id:
            query &= db.token_usage.user_id == user_id
        elif organization_id:
            query &= db.token_usage.organization_id == organization_id

        records = db(query).select()

        stats = UsageStats()
        for record in records:
            stats.total_tokens += record.waddleai_tokens or 0
            stats.input_tokens += record.tokens_input_total or 0
            stats.output_tokens += record.tokens_output_total or 0
            stats.request_count += record.request_count or 0
            stats.cost_usd += record.cost_usd_total or 0

            day_key = record.date.isoformat()
            stats.by_day[day_key] = stats.by_day.get(day_key, 0) + (record.waddleai_tokens or 0)

        return stats

    # Quota Management

    def check_quota(self, key_id: int) -> Tuple[bool, QuotaInfo]:
        """
        Check if a key has available quota.

        Returns:
            (allowed, quota_info) tuple
        """
        db = self.db

        key = db(db.virtual_keys.id == key_id).select().first()
        if not key:
            return False, QuotaInfo(status=QuotaStatus.DISABLED)

        if not key.enabled:
            return False, QuotaInfo(status=QuotaStatus.DISABLED)

        # Check daily budget
        today = date.today()
        daily_usage = db((db.token_usage.virtual_key_id == key_id) & (db.token_usage.date == today)).select().first()

        daily_cost = daily_usage.cost_usd_total if daily_usage else 0

        if key.budget_limit_daily and daily_cost >= key.budget_limit_daily:
            return False, QuotaInfo(
                status=QuotaStatus.EXCEEDED,
                limit=key.budget_limit_daily,
                used=daily_cost,
                remaining=0,
                percentage=100.0,
                resets_at=datetime.combine(today + timedelta(days=1), datetime.min.time()),
            )

        # Check monthly budget
        month_start = today.replace(day=1)
        monthly_usage = db((db.token_usage.virtual_key_id == key_id) & (db.token_usage.date >= month_start)).select()

        monthly_cost = sum(u.cost_usd_total or 0 for u in monthly_usage)

        if key.budget_limit_monthly and monthly_cost >= key.budget_limit_monthly:
            next_month = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
            return False, QuotaInfo(
                status=QuotaStatus.EXCEEDED,
                limit=key.budget_limit_monthly,
                used=monthly_cost,
                remaining=0,
                percentage=100.0,
                resets_at=datetime.combine(next_month, datetime.min.time()),
            )

        # Calculate quota status
        if key.budget_limit_monthly:
            percentage = (monthly_cost / key.budget_limit_monthly) * 100
            remaining = key.budget_limit_monthly - monthly_cost
            status = QuotaStatus.WARNING if percentage > 80 else QuotaStatus.OK

            return True, QuotaInfo(
                status=status,
                limit=key.budget_limit_monthly,
                used=monthly_cost,
                remaining=remaining,
                percentage=percentage,
            )

        return True, QuotaInfo(status=QuotaStatus.OK)

    def check_user_quota(self, user_id: int) -> Tuple[bool, QuotaInfo]:
        """Check user-level quota"""
        db = self.db

        user = db(db.users.id == user_id).select().first()
        if not user:
            return False, QuotaInfo(status=QuotaStatus.DISABLED)

        today = date.today()

        # Daily quota
        if user.token_quota_daily:
            daily_usage = db((db.token_usage.user_id == user_id) & (db.token_usage.date == today)).select()
            daily_tokens = sum(u.waddleai_tokens or 0 for u in daily_usage)

            if daily_tokens >= user.token_quota_daily:
                return False, QuotaInfo(
                    status=QuotaStatus.EXCEEDED,
                    limit=user.token_quota_daily,
                    used=daily_tokens,
                    remaining=0,
                    percentage=100.0,
                )

        # Monthly quota
        if user.token_quota_monthly:
            month_start = today.replace(day=1)
            monthly_usage = db((db.token_usage.user_id == user_id) & (db.token_usage.date >= month_start)).select()
            monthly_tokens = sum(u.waddleai_tokens or 0 for u in monthly_usage)

            if monthly_tokens >= user.token_quota_monthly:
                return False, QuotaInfo(
                    status=QuotaStatus.EXCEEDED,
                    limit=user.token_quota_monthly,
                    used=monthly_tokens,
                    remaining=0,
                    percentage=100.0,
                )

            percentage = (monthly_tokens / user.token_quota_monthly) * 100
            return True, QuotaInfo(
                status=QuotaStatus.WARNING if percentage > 80 else QuotaStatus.OK,
                limit=user.token_quota_monthly,
                used=monthly_tokens,
                remaining=user.token_quota_monthly - monthly_tokens,
                percentage=percentage,
            )

        return True, QuotaInfo(status=QuotaStatus.OK)

    def check_org_quota(self, org_id: int) -> Tuple[bool, QuotaInfo]:
        """Check organization-level quota"""
        db = self.db

        org = db(db.organizations.id == org_id).select().first()
        if not org:
            return False, QuotaInfo(status=QuotaStatus.DISABLED)

        today = date.today()
        month_start = today.replace(day=1)

        # Monthly quota
        if org.token_quota_monthly:
            monthly_usage = db(
                (db.token_usage.organization_id == org_id) & (db.token_usage.date >= month_start)
            ).select()
            monthly_tokens = sum(u.waddleai_tokens or 0 for u in monthly_usage)

            if monthly_tokens >= org.token_quota_monthly:
                return False, QuotaInfo(
                    status=QuotaStatus.EXCEEDED,
                    limit=org.token_quota_monthly,
                    used=monthly_tokens,
                    remaining=0,
                    percentage=100.0,
                )

            percentage = (monthly_tokens / org.token_quota_monthly) * 100
            return True, QuotaInfo(
                status=QuotaStatus.WARNING if percentage > 80 else QuotaStatus.OK,
                limit=org.token_quota_monthly,
                used=monthly_tokens,
                remaining=org.token_quota_monthly - monthly_tokens,
                percentage=percentage,
            )

        return True, QuotaInfo(status=QuotaStatus.OK)
