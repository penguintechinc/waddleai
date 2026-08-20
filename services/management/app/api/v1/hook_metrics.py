"""WaddleAI Management API v1 - Agent Hooks Metrics (§18 visibility surface).

Reads the in-process Prometheus counters/histogram `shared.utils.metrics`
already records for every `/hooks/evaluate` call and exposes them as one
JSON aggregate, `GET /api/v1/hooks/metrics`, attached to the shared
`hooks_bp` (see `hooks.py`). Before this route existed, the recorded
metrics were reachable only by scraping the raw Prometheus registry
in-process -- `/metrics` on this service is a small hand-rolled endpoint
that does not call `generate_latest()`, so none of the `waddleai_hook_*`
series were actually exposed anywhere an operator (or a Prometheus server)
could read them. This route is the fix: it reads the same counters
in-process via `.collect()` and returns them pre-aggregated.

Two sections, scoped differently because the underlying counters carry
different label granularity:

- `rule_hits`: per-rule match/decision counts. Computed only for the
  `hook_rules` rows the caller can already see (`scope_readable`, the
  exact same boundary `GET /rules` uses) -- genuinely org-scoped, because
  `rule_id` ties back to a row with a real `scope_type`/`scope_ref`.
- `platform`: ecosystem/event/decision breakdown, evaluation-latency
  percentiles, and fail-open/fail-closed counts. These counters carry NO
  organization label -- the §18 instrumentation is deployment-wide, not
  per-tenant -- so showing them to a `resource_manager` would leak
  cross-org aggregate behavior that isn't theirs to see. This section is
  therefore `admin`-only and is omitted (`null`), never zeroed or
  fabricated, for anyone else: an honest absence beats a fake org slice.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from quart import g, jsonify

from shared.auth.rbac import Permission
from shared.utils.metrics import get_management_metrics

from ...extensions import db
from .auth import require_auth, require_scope
from .hook_rules import scope_readable
from .hooks import hooks_bp

_DECISIONS = ("allow", "deny", "ask")


def _counter_samples(counter: Any) -> list[Any]:
    """Flatten a Counter's `.collect()` output, dropping the `_created` bookkeeping sample.

    A Counter's `MetricFamily.name` is the *bare* name (`waddleai_foo`), while
    its actual value sample is suffixed `_total` (`waddleai_foo_total`) --
    they never compare equal, so filtering on `_created` (the only other
    sample a Counter emits) is what actually isolates the value sample.
    """
    return [
        sample
        for family in counter.collect()
        for sample in family.samples
        if not sample.name.endswith("_created")
    ]


def _histogram_percentiles(
    histogram: Any, quantiles: tuple[float, ...] = (0.5, 0.95, 0.99)
) -> dict[str, float] | None:
    """Approximate p50/p95/p99 (in ms) from a Histogram's cumulative buckets.

    Linear interpolation within the bucket straddling each target rank --
    the same approach Prometheus's own `histogram_quantile()` uses. This is
    an approximation bounded by bucket width, not an exact quantile; good
    enough for an operator dashboard, and the only option available without
    a real Prometheus query engine sitting in front of this process.
    Returns `None` when the histogram has never observed a value.
    """
    buckets: dict[float, float] = {}
    total_count = 0.0
    total_sum = 0.0
    for family in histogram.collect():
        for sample in family.samples:
            if sample.name.endswith("_bucket"):
                le_raw = sample.labels.get("le", "+Inf")
                le = float("inf") if le_raw == "+Inf" else float(le_raw)
                buckets[le] = buckets.get(le, 0.0) + sample.value
            elif sample.name.endswith("_count"):
                total_count += sample.value
            elif sample.name.endswith("_sum"):
                total_sum += sample.value

    if total_count == 0:
        return None

    sorted_les = sorted(buckets)
    result: dict[str, float] = {}
    for q in quantiles:
        target = q * total_count
        prev_le, prev_count = 0.0, 0.0
        chosen = sorted_les[-1] if sorted_les else 0.0
        for le in sorted_les:
            count = buckets[le]
            if count >= target:
                if le == float("inf") or count == prev_count:
                    chosen = prev_le
                else:
                    frac = (target - prev_count) / (count - prev_count)
                    chosen = prev_le + frac * (le - prev_le)
                break
            prev_le, prev_count = le, count
        result[f"p{int(q * 100)}_ms"] = round(chosen * 1000, 3)

    result["sample_count"] = float(int(total_count))
    result["avg_ms"] = round((total_sum / total_count) * 1000, 3)
    return result


@hooks_bp.route("/metrics", methods=["GET"])
@require_auth
@require_scope(Permission.HOOK_METRICS_READ)
async def get_hook_metrics() -> tuple:
    """GET /api/v1/hooks/metrics -- rule hit-rates + (admin-only) platform latency/decisions."""
    user_role = g.user.get("role")
    user_org_id = g.user.get("organization_id")
    metrics = get_management_metrics()

    def _visible_rules() -> list[Any]:
        rows = db(db.hook_rules.id > 0).select(orderby=db.hook_rules.id)
        return [
            r for r in rows if scope_readable(user_role, user_org_id, r.scope_type, r.scope_ref)
        ]

    rows = await asyncio.to_thread(_visible_rules)

    eval_samples = _counter_samples(metrics.hook_rule_evaluations_total)
    decision_samples = _counter_samples(metrics.hook_rule_decisions_total)

    def _matched_count(rule_id: str, scope: str) -> int:
        return int(
            sum(
                s.value
                for s in eval_samples
                if s.labels.get("rule_id") == rule_id and s.labels.get("scope") == scope
            )
        )

    def _decided_counts(rule_id: str, scope: str) -> dict[str, int]:
        return {
            d: int(
                sum(
                    s.value
                    for s in decision_samples
                    if s.labels.get("rule_id") == rule_id
                    and s.labels.get("scope") == scope
                    and s.labels.get("decision") == d
                )
            )
            for d in _DECISIONS
        }

    rule_hits = [
        {
            "rule_id": str(r.id),
            "scope_type": r.scope_type,
            "scope_ref": r.scope_ref,
            "ecosystem": r.ecosystem,
            "event": r.event,
            "decision": r.decision,
            "enabled": r.enabled,
            "reason": r.reason,
            "matched": _matched_count(str(r.id), r.scope_type),
            "decided": _decided_counts(str(r.id), r.scope_type),
        }
        for r in rows
    ]

    platform: dict[str, Any] | None = None
    if user_role == "admin":
        invocations = [
            {
                "ecosystem": s.labels.get("ecosystem"),
                "event": s.labels.get("event"),
                "decision": s.labels.get("decision"),
                "count": int(s.value),
            }
            for s in _counter_samples(metrics.hook_invocations_total)
        ]

        fail_mode = {"fail_open": 0, "fail_closed": 0}
        for s in _counter_samples(metrics.hook_fail_mode_total):
            mode = s.labels.get("mode")
            if mode in fail_mode:
                fail_mode[mode] = int(s.value)

        timeout_samples = _counter_samples(metrics.hook_timeouts_total)
        timeouts = {s.labels.get("tier"): int(s.value) for s in timeout_samples}

        platform = {
            "invocations": invocations,
            "evaluation_latency": _histogram_percentiles(metrics.hook_evaluation_duration_seconds),
            "fail_mode": fail_mode,
            "timeouts": timeouts,
        }

    return (
        jsonify(
            {
                "status": "success",
                "data": {"rule_hits": rule_hits, "platform": platform},
                "meta": {"timestamp": datetime.utcnow().isoformat() + "Z"},
            }
        ),
        200,
    )
