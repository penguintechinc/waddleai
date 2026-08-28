"""Tests for the NER tier's licence/flag gate and disabled-entity/pattern caches.

Covers `_ner_tier_enabled` (cache hit, TTL expiry, checker-error fallback,
never-checked-yet fallback), `_check_ner_tier_entitlement` (flag/licence
branches), `_load_disabled_builtins`, `_load_disabled_ner_entities`
(cache + DB query, both against the real sqlite `content_filter_config`
table), and `_run_ner_patterns`'s disabled-entity skip and process-pool
failure branches. All TTL behaviour is driven by monkeypatching
`time.monotonic` rather than sleeping.
"""

from __future__ import annotations

import json
import logging
import time

import pytest
from penguin_dal import DAL

from shared.security import content_filter as content_filter_module
from shared.security.content_filter import ContentFilter


@pytest.fixture
def filter_instance(content_filter_db: DAL) -> ContentFilter:
    """A content filter backed by the real sqlite content-filter tables."""
    return ContentFilter(db=content_filter_db)


class _LicensedForNER:
    """Licence stub entitling the NER tier feature.

    Duplicated locally (rather than imported from `conftest.py`) because
    `tests/unit/` deliberately has no `__init__.py` in several subpackages
    (see `tests/unit/mcp/`) -- importing a plain class across test modules
    as `tests.unit.security.conftest.X` is fragile under that layout, while
    pytest's fixture-injection mechanism for `conftest.py` is not.
    """

    def check_feature(self, _feature: str) -> bool:
        return True


class _UnlicensedForNER:
    """Licence stub explicitly denying the NER tier feature entitlement."""

    def check_feature(self, _feature: str) -> bool:
        return False


class _FeatureFlags:
    """Fake feature-flag helper exposing only `is_feature_enabled`."""

    def __init__(self, enabled: bool) -> None:
        self._enabled = enabled

    def is_feature_enabled(self, _flag: str, distinct_id: str) -> bool:
        return self._enabled


class TestCheckNerTierEntitlement:
    """`_check_ner_tier_entitlement`'s flag/licence branches (sync, called via to_thread)."""

    def test_no_features_helper_and_no_license_client_is_disabled(self) -> None:
        """Neither a feature helper nor a licence client -> disabled (safe default)."""
        cf = ContentFilter(db=None)
        assert cf._check_ner_tier_entitlement(org_id=1) is False

    def test_feature_flag_off_short_circuits_before_license_check(self) -> None:
        """A feature helper reporting the flag OFF disables the tier without a licence check."""

        class _BoomIfCalled:
            def check_feature(self, _feature: str) -> bool:
                raise AssertionError("license client should not be consulted when flag is off")

        cf = ContentFilter(
            db=None, features=_FeatureFlags(enabled=False), license_client=_BoomIfCalled()
        )

        assert cf._check_ner_tier_entitlement(org_id=1) is False

    def test_flag_on_but_no_license_client_is_disabled(self) -> None:
        """Flag ON but no licence client configured -> still disabled."""
        cf = ContentFilter(db=None, features=_FeatureFlags(enabled=True), license_client=None)

        assert cf._check_ner_tier_entitlement(org_id=1) is False

    def test_flag_on_and_license_entitled_is_enabled(self) -> None:
        """Both gates open -> entitled."""
        cf = ContentFilter(
            db=None, features=_FeatureFlags(enabled=True), license_client=_LicensedForNER()
        )

        assert cf._check_ner_tier_entitlement(org_id=1) is True

    def test_flag_on_and_license_denies_is_disabled(self) -> None:
        """Flag open but licence explicitly denies the feature -> disabled."""
        cf = ContentFilter(
            db=None, features=_FeatureFlags(enabled=True), license_client=_UnlicensedForNER()
        )

        assert cf._check_ner_tier_entitlement(org_id=1) is False


class TestNerTierEnabledCaching:
    """`_ner_tier_enabled`'s per-org TTL cache and error-fallback semantics."""

    async def test_first_call_checks_and_caches(self) -> None:
        """First call performs the entitlement check and populates the cache."""
        cf = ContentFilter(db=None, license_client=_LicensedForNER())

        enabled = await cf._ner_tier_enabled(org_id=1)

        assert enabled is True
        assert cf._ner_tier_cache[1].enabled is True

    async def test_repeated_call_within_ttl_hits_cache_not_the_checker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second call inside the TTL window does not re-invoke the entitlement checker."""
        cf = ContentFilter(db=None, license_client=_LicensedForNER())
        await cf._ner_tier_enabled(org_id=1)

        def _boom(_org_id: int | None) -> bool:
            raise AssertionError("entitlement checker should not run again within the TTL")

        monkeypatch.setattr(cf, "_check_ner_tier_entitlement", _boom)

        assert await cf._ner_tier_enabled(org_id=1) is True

    async def test_cache_expiry_after_ttl_re_checks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Once the TTL elapses, the gate re-checks and can pick up a revoked entitlement."""
        cf = ContentFilter(db=None, license_client=_LicensedForNER())
        assert await cf._ner_tier_enabled(org_id=1) is True

        # Revoke entitlement and advance the monotonic clock past the TTL.
        cf._license_client = _UnlicensedForNER()
        real_monotonic = time.monotonic
        monkeypatch.setattr(
            time,
            "monotonic",
            lambda: real_monotonic() + content_filter_module._NER_TIER_CACHE_TTL + 1,
        )

        assert await cf._ner_tier_enabled(org_id=1) is False

    async def test_checker_error_falls_back_to_cached_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A checker failure after a successful prior check reuses the last-known value."""
        cf = ContentFilter(db=None, license_client=_LicensedForNER())
        assert await cf._ner_tier_enabled(org_id=1) is True

        def _boom(_org_id: int | None) -> bool:
            raise RuntimeError("licence server unreachable")

        monkeypatch.setattr(cf, "_check_ner_tier_entitlement", _boom)
        real_monotonic = time.monotonic
        monkeypatch.setattr(
            time,
            "monotonic",
            lambda: real_monotonic() + content_filter_module._NER_TIER_CACHE_TTL + 1,
        )

        assert await cf._ner_tier_enabled(org_id=1) is True  # falls back to last-known

    async def test_checker_error_with_no_prior_check_falls_back_to_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A checker failure with nothing cached yet defaults to disabled, never enabled."""
        cf = ContentFilter(db=None, license_client=_LicensedForNER())

        def _boom(_org_id: int | None) -> bool:
            raise RuntimeError("licence server unreachable")

        monkeypatch.setattr(cf, "_check_ner_tier_entitlement", _boom)

        assert await cf._ner_tier_enabled(org_id=99) is False


class TestLoadDisabledBuiltins:
    """`_load_disabled_builtins`'s TTL cache and DB-backed union of global/org config."""

    async def test_no_config_returns_empty_set(self, filter_instance: ContentFilter) -> None:
        """An empty content_filter_config table disables nothing."""
        assert await filter_instance._load_disabled_builtins(org_id=None) == set()

    async def test_global_disable_list_applies_with_no_org(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """A global (org_id=None) disabled_builtins row is honored for org_id=None."""
        content_filter_db.content_filter_config.insert(
            key="disabled_builtins", value=json.dumps(["email", "phone_us"]), organization_id=None
        )

        disabled = await filter_instance._load_disabled_builtins(org_id=None)

        assert disabled == {"email", "phone_us"}

    async def test_global_and_org_disable_lists_are_unioned(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """An org's disabled set includes both its own and the global list."""
        content_filter_db.content_filter_config.insert(
            key="disabled_builtins", value=json.dumps(["email"]), organization_id=None
        )
        content_filter_db.content_filter_config.insert(
            key="disabled_builtins", value=json.dumps(["ssn"]), organization_id=5
        )

        disabled = await filter_instance._load_disabled_builtins(org_id=5)

        assert disabled == {"email", "ssn"}

    async def test_malformed_json_value_is_ignored(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """A non-JSON config value degrades to no disabled patterns, not a raised exception."""
        content_filter_db.content_filter_config.insert(
            key="disabled_builtins", value="not valid json{", organization_id=None
        )

        assert await filter_instance._load_disabled_builtins(org_id=None) == set()

    async def test_non_list_json_value_is_ignored(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """A JSON value that parses but isn't a list (e.g. a dict) is ignored."""
        content_filter_db.content_filter_config.insert(
            key="disabled_builtins", value=json.dumps({"not": "a list"}), organization_id=None
        )

        assert await filter_instance._load_disabled_builtins(org_id=None) == set()

    async def test_repeated_call_within_ttl_hits_cache(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """A second call inside the TTL window returns the cached set, ignoring new DB rows."""
        first = await filter_instance._load_disabled_builtins(org_id=None)
        assert first == set()

        content_filter_db.content_filter_config.insert(
            key="disabled_builtins", value=json.dumps(["email"]), organization_id=None
        )

        second = await filter_instance._load_disabled_builtins(org_id=None)
        assert second == set()

    async def test_cache_expiry_after_ttl_re_queries_the_db(
        self,
        filter_instance: ContentFilter,
        content_filter_db: DAL,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Once the TTL window elapses, the next call re-queries and picks up new rows."""
        first = await filter_instance._load_disabled_builtins(org_id=None)
        assert first == set()

        content_filter_db.content_filter_config.insert(
            key="disabled_builtins", value=json.dumps(["email"]), organization_id=None
        )
        real_monotonic = time.monotonic
        monkeypatch.setattr(
            time, "monotonic", lambda: real_monotonic() + filter_instance.rule_cache_ttl + 1
        )

        second = await filter_instance._load_disabled_builtins(org_id=None)
        assert second == {"email"}

    async def test_org_scope_with_no_row_still_falls_through_to_global_scope(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """An org with no builtins-override row of its own still picks up the global list.

        Exercises the loop's "no matching row for this scope" branch on a
        non-final scope (org-specific), which must still continue on to the
        global scope rather than stopping early.
        """
        content_filter_db.content_filter_config.insert(
            key="disabled_builtins", value=json.dumps(["email"]), organization_id=None
        )

        disabled = await filter_instance._load_disabled_builtins(org_id=123)

        assert disabled == {"email"}

    async def test_db_error_fails_open_to_empty_set(self, caplog: pytest.LogCaptureFixture) -> None:
        """A broken db degrades to no disabled patterns rather than raising."""

        class _BrokenDB:
            def __getattr__(self, name: str) -> None:
                raise RuntimeError("db down")

        cf = ContentFilter(db=_BrokenDB())

        with caplog.at_level(logging.WARNING):
            disabled = await cf._load_disabled_builtins(org_id=3)

        assert disabled == set()
        assert "Failed to load disabled builtins for org 3" in caplog.text


class TestLoadDisabledNerEntities:
    """`_load_disabled_ner_entities` mirrors `_load_disabled_builtins`'s cache/query shape."""

    async def test_no_config_returns_empty_set(self, filter_instance: ContentFilter) -> None:
        """An empty content_filter_config table disables no NER entity types."""
        assert await filter_instance._load_disabled_ner_entities(org_id=None) == set()

    async def test_global_and_org_lists_are_unioned(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """Global and org-specific disabled_ner_entities rows are combined."""
        content_filter_db.content_filter_config.insert(
            key="disabled_ner_entities", value=json.dumps(["LOCATION"]), organization_id=None
        )
        content_filter_db.content_filter_config.insert(
            key="disabled_ner_entities", value=json.dumps(["PERSON"]), organization_id=8
        )

        disabled = await filter_instance._load_disabled_ner_entities(org_id=8)

        assert disabled == {"LOCATION", "PERSON"}

    async def test_org_scope_with_no_row_still_falls_through_to_global_scope(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """An org with no override row of its own still picks up the global disabled list.

        Exercises the loop's "no matching row for this scope" branch on the
        non-final (org-specific) scope, which must still continue on to the
        global scope rather than stopping early.
        """
        content_filter_db.content_filter_config.insert(
            key="disabled_ner_entities", value=json.dumps(["LOCATION"]), organization_id=None
        )

        disabled = await filter_instance._load_disabled_ner_entities(org_id=456)

        assert disabled == {"LOCATION"}

    async def test_repeated_call_within_ttl_hits_cache(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """A second call inside the TTL window returns the cached set."""
        first = await filter_instance._load_disabled_ner_entities(org_id=None)
        assert first == set()

        content_filter_db.content_filter_config.insert(
            key="disabled_ner_entities", value=json.dumps(["PERSON"]), organization_id=None
        )

        second = await filter_instance._load_disabled_ner_entities(org_id=None)
        assert second == set()

    async def test_cache_expiry_after_ttl_re_queries(
        self,
        filter_instance: ContentFilter,
        content_filter_db: DAL,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After the TTL elapses, a newly-added disabled entity is picked up."""
        first = await filter_instance._load_disabled_ner_entities(org_id=None)
        assert first == set()

        content_filter_db.content_filter_config.insert(
            key="disabled_ner_entities", value=json.dumps(["PERSON"]), organization_id=None
        )
        real_monotonic = time.monotonic
        monkeypatch.setattr(
            time, "monotonic", lambda: real_monotonic() + filter_instance.rule_cache_ttl + 1
        )

        second = await filter_instance._load_disabled_ner_entities(org_id=None)
        assert second == {"PERSON"}

    async def test_malformed_json_value_is_ignored(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """A non-JSON config value degrades to no disabled entity types."""
        content_filter_db.content_filter_config.insert(
            key="disabled_ner_entities", value="{not json", organization_id=None
        )

        assert await filter_instance._load_disabled_ner_entities(org_id=None) == set()

    async def test_non_list_json_value_is_ignored(
        self, filter_instance: ContentFilter, content_filter_db: DAL
    ) -> None:
        """A JSON value that parses but isn't a list (e.g. a dict) is ignored."""
        content_filter_db.content_filter_config.insert(
            key="disabled_ner_entities", value=json.dumps({"not": "a list"}), organization_id=None
        )

        assert await filter_instance._load_disabled_ner_entities(org_id=None) == set()

    async def test_db_error_fails_open_to_empty_set(self, caplog: pytest.LogCaptureFixture) -> None:
        """A broken db degrades to no disabled NER entity types rather than raising."""

        class _BrokenDB:
            def __getattr__(self, name: str) -> None:
                raise RuntimeError("db down")

        cf = ContentFilter(db=_BrokenDB())

        with caplog.at_level(logging.WARNING):
            disabled = await cf._load_disabled_ner_entities(org_id=None)

        assert disabled == set()
        assert "Failed to load disabled NER entities for org None" in caplog.text


class TestRunBuiltinPatternsDisabledSkip:
    """`_run_builtin_patterns` skips any pattern name present in the disabled set."""

    async def test_disabled_pattern_produces_no_violation_but_others_still_run(self) -> None:
        """Disabling 'email' skips it while 'ssn' still matches in the same text."""
        cf = ContentFilter(db=None)

        async def _disabled(_org_id: int | None) -> set[str]:
            return {"email"}

        cf._load_disabled_builtins = _disabled  # type: ignore[method-assign]

        text = "contact me at a@b.com, ssn is 123-45-6789"
        violations = await cf._run_builtin_patterns(text, "input", org_id=None)

        rule_names = {v.rule_name for v in violations}
        assert "email" not in rule_names
        assert "ssn" in rule_names


class TestRunNerPatternsDisabledAndFailures:
    """`_run_ner_patterns`'s disabled-entity skip and process-pool failure branch."""

    def _make_filter(self, monkeypatch: pytest.MonkeyPatch, entities: list[dict]) -> ContentFilter:
        """A filter with a fake NER backend wired to `entities`, using an inline executor."""

        class _InlineExecutor:
            def submit(self, fn: object, *args: object) -> object:
                import concurrent.futures

                future: concurrent.futures.Future = concurrent.futures.Future()
                try:
                    future.set_result(fn(*args))  # type: ignore[operator]
                except BaseException as exc:  # noqa: BLE001 - propagate via the Future
                    future.set_exception(exc)
                return future

        monkeypatch.setenv("WADDLEAI_STUB_UPSTREAM", "1")
        cf = ContentFilter(db=None, license_client=_LicensedForNER())
        cf.ner_filter = object()  # non-None sentinel

        def _fake_ner_analyze(_text: str) -> list[dict]:
            return entities

        monkeypatch.setattr(content_filter_module, "ner_analyze", _fake_ner_analyze)
        monkeypatch.setattr(content_filter_module, "_get_ner_pool", lambda: _InlineExecutor())
        return cf

    async def test_disabled_entity_type_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An entity type in the org's disabled set produces no violation."""
        cf = self._make_filter(
            monkeypatch,
            [{"entity_type": "PERSON", "text": "Jane Doe", "start": 0, "end": 8, "score": 0.9}],
        )

        async def _disabled(_org_id: int | None) -> set[str]:
            return {"PERSON"}

        cf._load_disabled_ner_entities = _disabled  # type: ignore[method-assign]

        violations = await cf._run_ner_patterns("Jane Doe called", "input", org_id=None)

        assert violations == []

    async def test_process_pool_failure_fails_open_to_no_violations(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A crash submitting to the process pool degrades to no NER violations, not a raise."""
        cf = self._make_filter(monkeypatch, [])

        def _boom(_text: str) -> list[dict]:
            raise RuntimeError("worker crashed")

        monkeypatch.setattr(content_filter_module, "ner_analyze", _boom)

        with caplog.at_level(logging.WARNING):
            violations = await cf._run_ner_patterns("some text", "input", org_id=None)

        assert violations == []
        assert "NER pattern run failed" in caplog.text
