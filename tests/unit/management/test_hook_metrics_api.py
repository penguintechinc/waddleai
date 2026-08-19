"""Unit tests for GET /api/v1/hooks/metrics (§18 visibility surface).

Covers: role gating (plain user 403), the §18.4 tenant-isolation boundary
applied to `rule_hits` (resource_manager sees own-org + global only), the
admin-only `platform` section being genuinely absent (not zeroed) for a
resource_manager, and the pure percentile-interpolation math in isolation
from the shared/process-global Prometheus registry.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from prometheus_client import CollectorRegistry, Histogram

from services.management.app.api.v1.hook_metrics import _histogram_percentiles
from tests.unit.management.conftest import make_select_result


def _mock_rule_row(
    rule_id: int,
    scope_type: str = "org",
    scope_ref: str | None = "1",
    decision: str = "deny",
    ecosystem: str | None = None,
    event: str | None = None,
) -> MagicMock:
    """A MagicMock standing in for a db `hook_rules` row."""
    row = MagicMock()
    row.id = rule_id
    row.scope_type = scope_type
    row.scope_ref = scope_ref
    row.ecosystem = ecosystem
    row.event = event
    row.tool_name_pattern = None
    row.match_pattern = None
    row.decision = decision
    row.reason = "test reason"
    row.enabled = True
    row.priority = 100
    return row


class TestRoleGating:
    """GET /api/v1/hooks/metrics requires admin or resource_manager."""

    async def test_plain_user_forbidden(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """A plain-user token is rejected with 403."""
        resp = await client.get("/api/v1/hooks/metrics", headers=user_auth_headers)
        assert resp.status_code == 403


class TestRuleHitsScoping:
    """§18.4: rule_hits is filtered by the same boundary as GET /rules."""

    async def test_resource_manager_never_sees_another_orgs_rule_hits(
        self, client, app_mock_db: MagicMock, rm_org2_auth_headers: dict
    ) -> None:
        """§18.4: a rule scoped to org 1 is invisible to a resource_manager of org 2."""
        org1_rule = _mock_rule_row(rule_id=90001, scope_type="org", scope_ref="1")
        global_rule = _mock_rule_row(rule_id=90002, scope_type="global", scope_ref=None)
        app_mock_db.return_value.select.return_value = make_select_result([org1_rule, global_rule])

        resp = await client.get("/api/v1/hooks/metrics", headers=rm_org2_auth_headers)

        assert resp.status_code == 200
        data = await resp.get_json()
        ids = {r["rule_id"] for r in data["data"]["rule_hits"]}
        assert "90001" not in ids  # org 1's rule invisible to org 2's resource_manager
        assert "90002" in ids  # global rule is visible (read-only) to every org

    async def test_resource_manager_platform_section_is_absent_not_zeroed(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """A resource_manager's response has `platform: null`, never a fabricated zeroed slice."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get("/api/v1/hooks/metrics", headers=rm_auth_headers)

        data = await resp.get_json()
        assert data["data"]["platform"] is None

    async def test_admin_sees_platform_section_with_expected_shape(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A global admin's `platform` section carries the full deployment-wide breakdown."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get("/api/v1/hooks/metrics", headers=auth_headers)

        assert resp.status_code == 200
        data = await resp.get_json()
        platform = data["data"]["platform"]
        assert platform is not None
        expected_keys = {"invocations", "evaluation_latency", "fail_mode", "timeouts"}
        assert set(platform.keys()) == expected_keys
        assert set(platform["fail_mode"].keys()) == {"fail_open", "fail_closed"}


class TestRuleHitCounts:
    """Recorded `hook_rule_evaluations_total`/`hook_rule_decisions_total` surface per rule."""

    async def test_matched_and_decided_counts_reflect_recorded_metrics(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A rule's matched/decided counts reflect the Prometheus counters recorded for it."""
        from shared.utils.metrics import get_management_metrics

        metrics = get_management_metrics()
        rule_id = "90099"  # distinctive id, unused by any other test in this suite
        metrics.record_hook_rule_evaluation(rule_id, "org")
        metrics.record_hook_rule_evaluation(rule_id, "org")
        metrics.record_hook_rule_decision(rule_id, "org", "deny")

        row = _mock_rule_row(rule_id=90099, scope_type="org", scope_ref="1", decision="deny")
        app_mock_db.return_value.select.return_value = make_select_result([row])

        resp = await client.get("/api/v1/hooks/metrics", headers=auth_headers)

        data = await resp.get_json()
        hit = next(r for r in data["data"]["rule_hits"] if r["rule_id"] == rule_id)
        assert hit["matched"] >= 2
        assert hit["decided"]["deny"] >= 1
        assert hit["decided"]["allow"] == 0

    async def test_unfired_rule_reports_zero_not_missing(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A rule an admin authored but that has never matched still appears, at 0."""
        row = _mock_rule_row(rule_id=90100, scope_type="global", scope_ref=None)
        app_mock_db.return_value.select.return_value = make_select_result([row])

        resp = await client.get("/api/v1/hooks/metrics", headers=auth_headers)

        data = await resp.get_json()
        hit = next(r for r in data["data"]["rule_hits"] if r["rule_id"] == "90100")
        assert hit["matched"] == 0
        assert hit["decided"] == {"allow": 0, "deny": 0, "ask": 0}


class TestPlatformInvocations:
    """Admin-only ecosystem/event/decision breakdown reflects recorded invocations."""

    async def test_invocation_breakdown_includes_recorded_combination(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A recorded ecosystem/event/decision triple appears in the admin invocation breakdown."""
        from shared.utils.metrics import get_management_metrics

        metrics = get_management_metrics()
        # A rarely-used combination so this assertion isn't sensitive to
        # other tests incrementing common ecosystem/event/decision triples.
        metrics.record_hook_invocation("antigravity", "notification", "ask")

        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get("/api/v1/hooks/metrics", headers=auth_headers)

        data = await resp.get_json()

        def _is_match(i: dict) -> bool:
            return (
                i["ecosystem"] == "antigravity"
                and i["event"] == "notification"
                and i["decision"] == "ask"
            )

        matches = [i for i in data["data"]["platform"]["invocations"] if _is_match(i)]
        assert matches and matches[0]["count"] >= 1


class TestHistogramPercentiles:
    """`_histogram_percentiles` in isolation -- an independent Histogram/registry per test."""

    def test_returns_none_for_untouched_histogram(self) -> None:
        """No observations yet -- `None`, not a division-by-zero or a fake zero result."""
        hist = Histogram("test_empty_seconds", "unit test", registry=CollectorRegistry())
        assert _histogram_percentiles(hist) is None

    def test_p50_falls_in_the_bucket_holding_the_median_observation(self) -> None:
        """Linear interpolation places p50 in the bucket that actually holds the median."""
        hist = Histogram(
            "test_latency_seconds",
            "unit test",
            buckets=(0.001, 0.005, 0.01, 0.05, 0.1),
            registry=CollectorRegistry(),
        )
        # 10 observations, all at 0.002s except one outlier at 0.08s -- median
        # (p50) must land in the (0.001, 0.005] bucket, p99 must reach past it.
        for _ in range(9):
            hist.observe(0.002)
        hist.observe(0.08)

        result = _histogram_percentiles(hist)

        assert result is not None
        assert 1.0 <= result["p50_ms"] <= 5.0
        assert result["p99_ms"] > result["p50_ms"]
        assert result["sample_count"] == 10.0

    def test_all_observations_at_bucket_ceiling_reports_that_ceiling(self) -> None:
        """A degenerate (zero-spread) distribution still returns a finite, sane percentile."""
        hist = Histogram(
            "test_uniform_seconds",
            "unit test",
            buckets=(0.01, 0.02),
            registry=CollectorRegistry(),
        )
        hist.observe(0.005)
        hist.observe(0.005)

        result = _histogram_percentiles(hist)

        assert result is not None
        assert result["p50_ms"] <= 10.0
