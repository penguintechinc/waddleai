"""Unit tests for shared.utils.token_manager.TokenManager.

Covers the surface not already exercised by test_token_manager_costmodel.py
(calculate_waddleai_tokens/calculate_cost): token counting/encoder routing,
conversion-rate loading, usage processing, the DB writes it triggers (new
record vs merge-into-existing across token_usage and both usage_cache
periods), quota enforcement, and usage stats aggregation.

Uses a minimal hand-written fake penguin-dal (see _FakeDB below) instead of
a spec-less Mock: a real object graph means a call to the classic PyDAL-only
Row.update_record() -- the exact bug this module's regression comments
describe -- raises AttributeError instead of silently succeeding, and the
existing-vs-new / merge-vs-add branches fall out of real dict/list state
instead of a hand-scripted sequence of .select() side_effect return values.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from unittest.mock import Mock

import pytest

from shared.utils.token_manager import TokenManager, TokenUsage, create_token_manager

# ---------------------------------------------------------------------------
# Fake penguin-dal (query-builder style: db(field == value).select()/.update())
# ---------------------------------------------------------------------------


class _Row:
    """A plain attribute bag -- deliberately NOT a Mock.

    Accessing an attribute that was never set (e.g. .update_record()) raises
    AttributeError, matching a real penguin_dal Row.
    """

    def __init__(self, **fields: object) -> None:
        self.__dict__.update(fields)


class _Rows(list):
    """list of _Row with the .first() accessor real penguin-dal result sets have."""

    def first(self) -> _Row | None:
        return self[0] if self else None


class _Predicate:
    """A composable, evaluatable stand-in for a PyDAL/penguin-dal Query object."""

    def __init__(self, table: _Table, fn) -> None:
        self.table = table
        self._fn = fn

    def __and__(self, other: _Predicate) -> _Predicate:
        assert self.table is other.table, "cross-table AND is not used by TokenManager"
        return _Predicate(self.table, lambda row: self._fn(row) and other._fn(row))

    def __call__(self, row: _Row) -> bool:
        return self._fn(row)


class _Field:
    """A stand-in for db.<table>.<field>, producing a _Predicate on comparison."""

    def __init__(self, table: _Table, name: str) -> None:
        self.table = table
        self.name = name

    def __eq__(self, other: object) -> _Predicate:  # type: ignore[override]
        return _Predicate(self.table, lambda row: getattr(row, self.name, None) == other)

    def __ge__(self, other: object) -> _Predicate:
        return _Predicate(self.table, lambda row: getattr(row, self.name, None) >= other)


class _QuerySet:
    """Result of db(predicate) -- supports .select() and .update(**kwargs)."""

    def __init__(self, table: _Table, predicate: _Predicate) -> None:
        self._table = table
        self._predicate = predicate

    def select(self) -> _Rows:
        return _Rows(r for r in self._table.rows if self._predicate(r))

    def update(self, **kwargs: object) -> int:
        matched = [r for r in self._table.rows if self._predicate(r)]
        for row in matched:
            row.__dict__.update(kwargs)
        return len(matched)


class _Table:
    """A fake table: db.insert(**kwargs) plus db.<table>.<field> access for queries."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.rows: list[_Row] = []
        self._next_id = 1

    def __getattr__(self, field_name: str) -> _Field:
        if field_name.startswith("__") and field_name.endswith("__"):
            raise AttributeError(field_name)
        return _Field(self, field_name)

    def insert(self, **kwargs: object) -> int:
        row_id = kwargs.pop("id", self._next_id)
        row = _Row(id=row_id, **kwargs)
        self.rows.append(row)
        self._next_id = max(self._next_id, row_id) + 1
        return row_id


class _FakeDB:
    """Fake db callable: db(predicate) -> _QuerySet; db.<table> -> _Table."""

    def __init__(self) -> None:
        self.token_usage = _Table("token_usage")
        self.usage_cache = _Table("usage_cache")
        self.api_keys = _Table("api_keys")
        self.users = _Table("users")
        self.token_conversion_rates = _Table("token_conversion_rates")

    def __call__(self, predicate: _Predicate) -> _QuerySet:
        return _QuerySet(predicate.table, predicate)


@pytest.fixture
def fake_db() -> _FakeDB:
    """A fresh in-memory fake penguin-dal handle for each test."""
    return _FakeDB()


# ---------------------------------------------------------------------------
# Fake, offline tiktoken encoder
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _FakeEncoder:
    """Deterministic, offline stand-in for a tiktoken Encoding.

    Token count = word count * multiplier, so tests can assert exactly which
    encoder TokenManager routed a given (provider, model) pair to.
    """

    multiplier: int = 1
    calls: int = 0

    def encode(self, text: str) -> list[str]:
        self.calls += 1
        return text.split() * self.multiplier


@pytest.fixture
def fake_encoders(monkeypatch: pytest.MonkeyPatch) -> dict[str, _FakeEncoder]:
    """Replace tiktoken.encoding_for_model with a deterministic, offline fake.

    TokenManager.__init__ eagerly builds encoders for gpt-4/gpt-3.5-turbo via
    tiktoken.encoding_for_model(), which downloads its BPE ranks file over
    the network on first use if not already cached -- a unit test must never
    depend on that.
    """
    registry: dict[str, _FakeEncoder] = {}

    def _encoding_for_model(model: str) -> _FakeEncoder:
        multiplier = 1 if model == "gpt-4" else 2
        return registry.setdefault(model, _FakeEncoder(multiplier))

    monkeypatch.setattr(
        "shared.utils.token_manager.tiktoken.encoding_for_model", _encoding_for_model
    )
    return registry


@pytest.fixture
def manager(fake_db: _FakeDB, fake_encoders: dict[str, _FakeEncoder]) -> TokenManager:
    """A TokenManager wired to the fake DAL and fake (offline) encoders."""
    return TokenManager(fake_db)


# ---------------------------------------------------------------------------
# count_tokens
# ---------------------------------------------------------------------------


class TestCountTokens:
    """count_tokens() routing across model families and its heuristic fallback."""

    @pytest.mark.parametrize(
        ("provider", "model", "expected_multiplier"),
        [
            ("openai", "gpt-4", 1),  # explicit encoder registered for this exact model
            ("openai", "gpt-3.5-turbo", 2),  # explicit encoder, different multiplier
            ("openai", "gpt-4o-mini", 2),  # known provider, unmapped model -> default_encoder
            ("anthropic", "claude-3.5-sonnet", 2),  # unmapped provider -> default_encoder
            ("ollama", "llama2", 2),  # unmapped provider -> default_encoder
        ],
    )
    def test_count_tokens_routes_by_provider_and_model(
        self, manager: TokenManager, provider: str, model: str, expected_multiplier: int
    ) -> None:
        """Each (provider, model) pair is routed to its own encoder, or the shared default."""
        count = manager.count_tokens("hello there friend", provider, model)
        assert count == 3 * expected_multiplier

    def test_falls_back_to_char_heuristic_on_encoder_error(
        self, manager: TokenManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the encoder raises, count_tokens degrades to a 4-chars-per-token estimate."""
        raising_encoder = Mock()
        raising_encoder.encode.side_effect = RuntimeError("boom")
        # default_encoder is a plain attribute (not a __slots__ field) on TokenManager
        # itself, so it can be swapped wholesale -- _FakeEncoder is slots=True and has
        # no .encode attribute to monkeypatch in place.
        monkeypatch.setattr(manager, "default_encoder", raising_encoder)
        text = "a" * 41
        assert manager.count_tokens(text, "unknown", "unknown-model") == len(text) // 4

    def test_unknown_provider_and_model_never_raises(self, manager: TokenManager) -> None:
        """An unrecognized provider/model is not an error -- it silently falls back to default."""
        count = manager.count_tokens("one two three four", "totally-unknown", "totally-unknown")
        assert count == 4 * 2


# ---------------------------------------------------------------------------
# _load_conversion_rates
# ---------------------------------------------------------------------------


class TestLoadConversionRates:
    """_load_conversion_rates(): DB-seeded rates vs the built-in defaults."""

    def test_falls_back_to_defaults_when_table_is_empty(
        self, fake_db: _FakeDB, fake_encoders: dict[str, _FakeEncoder]
    ) -> None:
        """An empty token_conversion_rates table seeds from DEFAULT_CONVERSION_RATES."""
        manager = TokenManager(fake_db)
        assert manager.conversion_rates == TokenManager.DEFAULT_CONVERSION_RATES

    def test_loads_rates_from_db_when_present(
        self, fake_db: _FakeDB, fake_encoders: dict[str, _FakeEncoder]
    ) -> None:
        """Enabled rows in the DB replace the defaults entirely."""
        fake_db.token_conversion_rates.insert(
            provider="openai",
            model="gpt-4",
            input_rate=5.0,
            output_rate=5.0,
            base_cost_per_waddleai_token=0.01,
            enabled=True,
        )
        manager = TokenManager(fake_db)
        assert set(manager.conversion_rates) == {"openai:gpt-4"}
        rate = manager.conversion_rates["openai:gpt-4"]
        assert (rate.input_rate, rate.output_rate, rate.base_cost_per_waddleai_token) == (
            5.0,
            5.0,
            0.01,
        )


# ---------------------------------------------------------------------------
# process_usage
# ---------------------------------------------------------------------------


class TestProcessUsage:
    """process_usage(): counts, WaddleAI conversion, cost, and the DB write it triggers."""

    def test_uses_actual_token_counts_when_provided_skipping_the_encoder(
        self, manager: TokenManager, fake_encoders: dict[str, _FakeEncoder]
    ) -> None:
        """actual_input_tokens/actual_output_tokens bypass count_tokens entirely."""
        usage = manager.process_usage(
            input_text="ignored",
            output_text="ignored",
            provider="openai",
            model="gpt-4",
            api_key_id=1,
            user_id=1,
            organization_id=1,
            actual_input_tokens=100,
            actual_output_tokens=50,
        )
        assert (usage.llm_tokens_input, usage.llm_tokens_output) == (100, 50)
        # encoding_for_model() ran during TokenManager.__init__, but .encode()
        # itself is never called when actual token counts are supplied.
        assert all(encoder.calls == 0 for encoder in fake_encoders.values())

    def test_falls_back_to_counting_when_actual_tokens_omitted(self, manager: TokenManager) -> None:
        """Without actual_*_tokens, process_usage estimates via count_tokens."""
        usage = manager.process_usage(
            input_text="hello there",
            output_text="hi",
            provider="openai",
            model="gpt-4",
            api_key_id=1,
            user_id=1,
            organization_id=1,
        )
        assert usage.llm_tokens_input == 2  # "hello there" -> 2 words, gpt-4 multiplier 1
        assert usage.llm_tokens_output == 1  # "hi" -> 1 word

    def test_builds_breakdown_keyed_by_sanitized_model_name(self, manager: TokenManager) -> None:
        """The breakdown key is `{provider}_{model with '-' replaced by '_'}`."""
        usage = manager.process_usage(
            input_text="x",
            output_text="y",
            provider="openai",
            model="gpt-3.5-turbo",
            api_key_id=1,
            user_id=1,
            organization_id=1,
            actual_input_tokens=10,
            actual_output_tokens=5,
        )
        assert usage.llm_tokens_breakdown == {"openai_gpt_3.5_turbo": {"input": 10, "output": 5}}

    def test_negative_actual_token_counts_never_raise_and_floor_waddleai_contribution(
        self, manager: TokenManager
    ) -> None:
        """Negative actual_*_tokens don't crash -- the `> 0` guard floors WaddleAI charge to 0."""
        usage = manager.process_usage(
            input_text="x",
            output_text="y",
            provider="openai",
            model="gpt-4",
            api_key_id=1,
            user_id=1,
            organization_id=1,
            actual_input_tokens=-10,
            actual_output_tokens=-5,
        )
        assert (usage.llm_tokens_input, usage.llm_tokens_output) == (-10, -5)
        assert usage.waddleai_tokens == 0

    def test_persists_via_update_usage_records(
        self, manager: TokenManager, fake_db: _FakeDB
    ) -> None:
        """process_usage writes one token_usage row and both usage_cache periods."""
        manager.process_usage(
            input_text="x",
            output_text="y",
            provider="openai",
            model="gpt-4",
            api_key_id=7,
            user_id=3,
            organization_id=9,
            actual_input_tokens=10,
            actual_output_tokens=5,
        )
        assert len(fake_db.token_usage.rows) == 1
        row = fake_db.token_usage.rows[0]
        assert (row.api_key_id, row.user_id, row.organization_id) == (7, 3, 9)
        assert len(fake_db.usage_cache.rows) == 2


# ---------------------------------------------------------------------------
# _update_usage_records / _update_usage_cache
# ---------------------------------------------------------------------------


def _usage(waddleai: int, inp: int, out: int, model_key: str) -> TokenUsage:
    return TokenUsage(
        waddleai_tokens=waddleai,
        llm_tokens_input=inp,
        llm_tokens_output=out,
        llm_tokens_breakdown={model_key: {"input": inp, "output": out}},
        cost_estimate_waddleai=float(waddleai),
        cost_estimate_usd=float(waddleai) * 0.001,
    )


class TestUsageAccumulation:
    """_update_usage_records/_update_usage_cache: new-record vs merge-into-existing branches.

    _update_usage_records always calls _update_usage_cache, so a single call
    exercises the insert branch of all three storage locations (token_usage,
    daily cache, monthly cache); a second same-day call exercises all three
    merge branches at once.
    """

    def test_first_call_inserts_new_daily_record_and_both_cache_periods(
        self, manager: TokenManager, fake_db: _FakeDB
    ) -> None:
        """A first call for a given api_key/day creates 1 token_usage row + 2 cache rows."""
        manager._update_usage_records(_usage(10, 5, 5, "openai_gpt_4"), 1, 1, 1, "openai", "gpt-4")
        assert len(fake_db.token_usage.rows) == 1
        row = fake_db.token_usage.rows[0]
        assert (row.waddleai_tokens, row.request_count) == (10, 1)

        assert len(fake_db.usage_cache.rows) == 2
        periods = {r.period for r in fake_db.usage_cache.rows}
        assert periods == {"daily", "monthly"}
        for cache_row in fake_db.usage_cache.rows:
            assert (cache_row.waddleai_tokens_used, cache_row.requests_made) == (10, 1)

    def test_second_call_same_day_same_model_merges_into_existing_breakdown(
        self, manager: TokenManager, fake_db: _FakeDB
    ) -> None:
        """A same-day, same-model second call merges counts into the existing breakdown key."""
        manager._update_usage_records(_usage(10, 5, 5, "openai_gpt_4"), 1, 1, 1, "openai", "gpt-4")
        manager._update_usage_records(_usage(6, 3, 3, "openai_gpt_4"), 1, 1, 1, "openai", "gpt-4")

        assert len(fake_db.token_usage.rows) == 1  # merged, not a second row
        row = fake_db.token_usage.rows[0]
        assert (row.waddleai_tokens, row.request_count) == (16, 2)
        assert json.loads(row.llm_tokens) == {"openai_gpt_4": {"input": 8, "output": 8}}

        assert len(fake_db.usage_cache.rows) == 2  # still just daily + monthly, merged in place
        for cache_row in fake_db.usage_cache.rows:
            assert (cache_row.waddleai_tokens_used, cache_row.requests_made) == (16, 2)
            merged = json.loads(cache_row.llm_tokens_used)
            assert merged == {"openai_gpt_4": {"input": 8, "output": 8}}

    def test_third_call_different_model_adds_new_breakdown_key_everywhere(
        self, manager: TokenManager, fake_db: _FakeDB
    ) -> None:
        """A different model on the same day adds a new breakdown key instead of merging."""
        manager._update_usage_records(_usage(10, 5, 5, "openai_gpt_4"), 1, 1, 1, "openai", "gpt-4")
        manager._update_usage_records(
            _usage(4, 2, 2, "anthropic_claude_3_opus"), 1, 1, 1, "anthropic", "claude-3-opus"
        )
        row = fake_db.token_usage.rows[0]
        assert set(json.loads(row.llm_tokens)) == {"openai_gpt_4", "anthropic_claude_3_opus"}
        assert row.request_count == 2

    def test_different_api_key_gets_its_own_record(
        self, manager: TokenManager, fake_db: _FakeDB
    ) -> None:
        """Two different api_key_ids on the same day never merge into one row."""
        manager._update_usage_records(_usage(10, 5, 5, "openai_gpt_4"), 1, 1, 1, "openai", "gpt-4")
        manager._update_usage_records(_usage(10, 5, 5, "openai_gpt_4"), 2, 1, 1, "openai", "gpt-4")
        assert len(fake_db.token_usage.rows) == 2
        assert len(fake_db.usage_cache.rows) == 4


# ---------------------------------------------------------------------------
# check_quota
# ---------------------------------------------------------------------------


class TestCheckQuota:
    """check_quota(): API-key/user resolution, limit precedence, and pass/fail combinations."""

    def _seed_key_and_user(
        self,
        fake_db: _FakeDB,
        *,
        api_key_daily: int | None = None,
        api_key_monthly: int | None = None,
        user_daily: int = 1000,
        user_monthly: int = 30000,
        user_id: int = 1,
        api_key_id: int = 1,
    ) -> None:
        fake_db.users.insert(
            id=user_id, token_quota_daily=user_daily, token_quota_monthly=user_monthly
        )
        fake_db.api_keys.insert(
            id=api_key_id,
            user_id=user_id,
            token_quota_daily=api_key_daily,
            token_quota_monthly=api_key_monthly,
        )

    def test_api_key_not_found_is_reported_and_fails_closed(self, manager: TokenManager) -> None:
        """An unknown api_key_id returns (False, {"error": ...}) rather than raising."""
        ok, info = manager.check_quota(api_key_id=999)
        assert (ok, info) == (False, {"error": "API key not found"})

    def test_user_not_found_is_reported_and_fails_closed(
        self, manager: TokenManager, fake_db: _FakeDB
    ) -> None:
        """An api_key pointing at a missing user also fails closed."""
        fake_db.api_keys.insert(id=1, user_id=42, token_quota_daily=None, token_quota_monthly=None)
        ok, info = manager.check_quota(api_key_id=1)
        assert (ok, info) == (False, {"error": "User not found"})

    def test_within_both_limits_with_no_usage_recorded_yet(
        self, manager: TokenManager, fake_db: _FakeDB
    ) -> None:
        """No usage_cache rows at all means used=0, which is always within limit."""
        self._seed_key_and_user(fake_db)
        ok, info = manager.check_quota(api_key_id=1)
        assert ok is True
        assert info["daily"] == {"used": 0, "limit": 1000, "remaining": 1000, "ok": True}
        assert info["monthly"]["ok"] is True

    def test_over_daily_limit_fails_even_if_monthly_ok(
        self, manager: TokenManager, fake_db: _FakeDB
    ) -> None:
        """Daily usage at/over the daily limit fails the overall check regardless of monthly."""
        self._seed_key_and_user(fake_db, user_daily=100)
        now = datetime.utcnow()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        fake_db.usage_cache.insert(
            api_key_id=1,
            organization_id=1,
            period="daily",
            period_start=today,
            waddleai_tokens_used=150,
            llm_tokens_used="{}",
            requests_made=1,
            last_updated=now,
        )
        ok, info = manager.check_quota(api_key_id=1)
        assert ok is False
        assert info["daily"]["ok"] is False
        assert info["monthly"]["ok"] is True

    def test_over_monthly_limit_fails_even_if_daily_ok(
        self, manager: TokenManager, fake_db: _FakeDB
    ) -> None:
        """Monthly usage at/over the monthly limit fails the overall check regardless of daily."""
        self._seed_key_and_user(fake_db, user_monthly=100)
        now = datetime.utcnow()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        fake_db.usage_cache.insert(
            api_key_id=1,
            organization_id=1,
            period="monthly",
            period_start=month_start,
            waddleai_tokens_used=150,
            llm_tokens_used="{}",
            requests_made=3,
            last_updated=now,
        )
        ok, info = manager.check_quota(api_key_id=1)
        assert ok is False
        assert info["monthly"]["ok"] is False
        assert info["daily"]["ok"] is True

    def test_api_key_quota_overrides_user_quota_when_set(
        self, manager: TokenManager, fake_db: _FakeDB
    ) -> None:
        """A non-null api_key.token_quota_daily wins over the user's own quota."""
        self._seed_key_and_user(fake_db, api_key_daily=50, user_daily=100000)
        now = datetime.utcnow()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        fake_db.usage_cache.insert(
            api_key_id=1,
            organization_id=1,
            period="daily",
            period_start=today,
            waddleai_tokens_used=60,
            llm_tokens_used="{}",
            requests_made=1,
            last_updated=now,
        )
        ok, info = manager.check_quota(api_key_id=1)
        assert info["daily"]["limit"] == 50
        assert ok is False  # 60 >= 50

    def test_falls_back_to_user_quota_when_api_key_quota_unset(
        self, manager: TokenManager, fake_db: _FakeDB
    ) -> None:
        """A null api_key.token_quota_daily falls back to the user's quota."""
        self._seed_key_and_user(fake_db, api_key_daily=None, user_daily=5000)
        _, info = manager.check_quota(api_key_id=1)
        assert info["daily"]["limit"] == 5000


# ---------------------------------------------------------------------------
# get_usage_stats
# ---------------------------------------------------------------------------


class TestGetUsageStats:
    """get_usage_stats(): filter precedence, aggregation, and the empty-result path."""

    def _insert(
        self,
        fake_db: _FakeDB,
        *,
        date_: date,
        waddleai: int,
        inp: int,
        out: int,
        requests: int,
        llm_tokens: str | None,
        api_key_id: int = 1,
        user_id: int = 1,
        organization_id: int = 1,
    ) -> None:
        fake_db.token_usage.insert(
            api_key_id=api_key_id,
            user_id=user_id,
            organization_id=organization_id,
            date=date_,
            waddleai_tokens=waddleai,
            tokens_input_total=inp,
            tokens_output_total=out,
            request_count=requests,
            llm_tokens=llm_tokens,
        )

    def test_no_matching_records_returns_zeroed_stats(self, manager: TokenManager) -> None:
        """An empty token_usage table produces all-zero stats, not a KeyError/crash."""
        stats = manager.get_usage_stats(api_key_id=1, days=30)
        assert stats["total_waddleai_tokens"] == 0
        assert stats["total_requests"] == 0
        assert stats["daily_usage"] == {}
        assert stats["llm_breakdown"] == {}
        assert stats["average_daily"] == 0

    def test_aggregates_across_records_and_computes_average(
        self, manager: TokenManager, fake_db: _FakeDB
    ) -> None:
        """Multiple records sum into totals, and average_daily = total // days."""
        today = date.today()
        self._insert(
            fake_db,
            date_=today,
            waddleai=100,
            inp=80,
            out=20,
            requests=2,
            llm_tokens=json.dumps({"openai_gpt_4": {"input": 80, "output": 20}}),
        )
        self._insert(
            fake_db,
            date_=today - timedelta(days=1),
            waddleai=50,
            inp=40,
            out=10,
            requests=1,
            llm_tokens=json.dumps({"openai_gpt_4": {"input": 40, "output": 10}}),
        )
        stats = manager.get_usage_stats(api_key_id=1, days=30)
        assert stats["total_waddleai_tokens"] == 150
        assert stats["total_llm_input_tokens"] == 120
        assert stats["total_llm_output_tokens"] == 30
        assert stats["total_requests"] == 3
        assert stats["llm_breakdown"] == {"openai_gpt_4": {"input": 120, "output": 30}}
        assert len(stats["daily_usage"]) == 2
        assert stats["average_daily"] == 150 // 30

    def test_merges_breakdown_across_different_models(
        self, manager: TokenManager, fake_db: _FakeDB
    ) -> None:
        """Records for different models both contribute their own llm_breakdown key."""
        today = date.today()
        self._insert(
            fake_db,
            date_=today,
            waddleai=10,
            inp=8,
            out=2,
            requests=1,
            llm_tokens=json.dumps({"openai_gpt_4": {"input": 8, "output": 2}}),
        )
        self._insert(
            fake_db,
            date_=today,
            waddleai=5,
            inp=4,
            out=1,
            requests=1,
            llm_tokens=json.dumps({"anthropic_claude_3_opus": {"input": 4, "output": 1}}),
        )
        stats = manager.get_usage_stats(api_key_id=1, days=30)
        assert set(stats["llm_breakdown"]) == {"openai_gpt_4", "anthropic_claude_3_opus"}

    def test_records_without_llm_tokens_are_skipped_in_breakdown(
        self, manager: TokenManager, fake_db: _FakeDB
    ) -> None:
        """A falsy llm_tokens column (None) is still counted in totals but skips the breakdown."""
        today = date.today()
        self._insert(fake_db, date_=today, waddleai=10, inp=8, out=2, requests=1, llm_tokens=None)
        stats = manager.get_usage_stats(api_key_id=1, days=30)
        assert stats["llm_breakdown"] == {}
        assert stats["total_waddleai_tokens"] == 10

    def test_records_outside_the_window_are_excluded(
        self, manager: TokenManager, fake_db: _FakeDB
    ) -> None:
        """A record older than `days` is excluded by the `date >= since` filter."""
        old = date.today() - timedelta(days=100)
        self._insert(fake_db, date_=old, waddleai=999, inp=1, out=1, requests=1, llm_tokens=None)
        stats = manager.get_usage_stats(api_key_id=1, days=30)
        assert stats["total_waddleai_tokens"] == 0

    def test_filters_by_api_key_id_when_given(
        self, manager: TokenManager, fake_db: _FakeDB
    ) -> None:
        """api_key_id, when given, scopes the query to that key alone."""
        today = date.today()
        self._insert(
            fake_db,
            date_=today,
            waddleai=10,
            inp=1,
            out=1,
            requests=1,
            llm_tokens=None,
            api_key_id=1,
        )
        self._insert(
            fake_db,
            date_=today,
            waddleai=20,
            inp=1,
            out=1,
            requests=1,
            llm_tokens=None,
            api_key_id=2,
        )
        stats = manager.get_usage_stats(api_key_id=1, days=30)
        assert stats["total_waddleai_tokens"] == 10

    def test_filters_by_user_id_when_api_key_id_omitted(
        self, manager: TokenManager, fake_db: _FakeDB
    ) -> None:
        """user_id scopes the query when api_key_id is not given."""
        today = date.today()
        self._insert(
            fake_db, date_=today, waddleai=10, inp=1, out=1, requests=1, llm_tokens=None, user_id=5
        )
        self._insert(
            fake_db, date_=today, waddleai=20, inp=1, out=1, requests=1, llm_tokens=None, user_id=6
        )
        stats = manager.get_usage_stats(user_id=5, days=30)
        assert stats["total_waddleai_tokens"] == 10

    def test_filters_by_organization_id_when_others_omitted(
        self, manager: TokenManager, fake_db: _FakeDB
    ) -> None:
        """organization_id scopes the query when neither api_key_id nor user_id is given."""
        today = date.today()
        self._insert(
            fake_db,
            date_=today,
            waddleai=10,
            inp=1,
            out=1,
            requests=1,
            llm_tokens=None,
            organization_id=7,
        )
        self._insert(
            fake_db,
            date_=today,
            waddleai=20,
            inp=1,
            out=1,
            requests=1,
            llm_tokens=None,
            organization_id=8,
        )
        stats = manager.get_usage_stats(organization_id=7, days=30)
        assert stats["total_waddleai_tokens"] == 10

    def test_api_key_id_takes_precedence_over_user_id_when_both_given(
        self, manager: TokenManager, fake_db: _FakeDB
    ) -> None:
        """The filter chain is elif-based: api_key_id wins, user_id is ignored if also passed."""
        today = date.today()
        self._insert(
            fake_db,
            date_=today,
            waddleai=10,
            inp=1,
            out=1,
            requests=1,
            llm_tokens=None,
            api_key_id=1,
            user_id=999,
        )
        self._insert(
            fake_db,
            date_=today,
            waddleai=20,
            inp=1,
            out=1,
            requests=1,
            llm_tokens=None,
            api_key_id=2,
            user_id=1,
        )
        stats = manager.get_usage_stats(api_key_id=1, user_id=1, days=30)
        assert stats["total_waddleai_tokens"] == 10  # only the api_key_id=1 row

    def test_no_filters_returns_all_records_within_window(
        self, manager: TokenManager, fake_db: _FakeDB
    ) -> None:
        """With no id filters given, only the date window applies."""
        today = date.today()
        self._insert(
            fake_db,
            date_=today,
            waddleai=10,
            inp=1,
            out=1,
            requests=1,
            llm_tokens=None,
            api_key_id=1,
        )
        self._insert(
            fake_db,
            date_=today,
            waddleai=20,
            inp=1,
            out=1,
            requests=1,
            llm_tokens=None,
            api_key_id=2,
        )
        stats = manager.get_usage_stats(days=30)
        assert stats["total_waddleai_tokens"] == 30


# ---------------------------------------------------------------------------
# create_token_manager factory
# ---------------------------------------------------------------------------


def test_create_token_manager_returns_wired_instance(
    fake_db: _FakeDB, fake_encoders: dict[str, _FakeEncoder]
) -> None:
    """The factory returns a TokenManager bound to the given db handle."""
    manager = create_token_manager(fake_db)
    assert isinstance(manager, TokenManager)
    assert manager.db is fake_db
