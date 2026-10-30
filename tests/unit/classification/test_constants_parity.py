"""Drift guard: the service's copied gate/fail-mode constants must equal the live source.

The gate and fail-mode policy are deliberate R2 copies of shared/security/content_filter.py
until P6 deletes it. Both sides are imported (not hardcoded), so a change to the source
fails here. Literal anchors pin the spec values as well.
"""

from __future__ import annotations

import inspect

from services.classification.app import failmode, gate
from services.classification.app.config import ClassificationConfig
from shared.security import content_filter


def test_gate_constants_equal_content_filter() -> None:
    """Flag, entitlement and cache TTL match ContentFilter's NER tier gate."""
    assert gate.NER_TIER_FLAG == content_filter.NER_TIER_FLAG == "waddleai.pii_ner"
    assert (
        gate.NER_TIER_LICENSE_FEATURE
        == content_filter.NER_TIER_LICENSE_FEATURE
        == "pii_ner_detection"
    )
    assert gate.NER_TIER_CACHE_TTL == content_filter._NER_TIER_CACHE_TTL == 60.0


def test_gate_falsy_distinct_id_default_matches_source() -> None:
    """ContentFilter maps a falsy org id to "server"; the gate's default must be the same."""
    src = inspect.getsource(content_filter.ContentFilter)
    assert 'distinct_id=str(org_id) if org_id else "server"' in src
    assert gate.DEFAULT_DISTINCT_ID == failmode.DEFAULT_DISTINCT_ID == "server"


def test_failmode_constants_equal_content_filter() -> None:
    """Kill-switch flag and its cache TTL match ContentFilter's."""
    assert (
        failmode.DISABLE_AUDITOR_FAIL_MODE_POLICY_FLAG
        == content_filter.DISABLE_AUDITOR_FAIL_MODE_POLICY_FLAG
        == "waddleai.disable-auditor-fail-mode-policy"
    )
    assert (
        failmode._AUDITOR_FAIL_MODE_FLAG_CACHE_TTL
        == content_filter._AUDITOR_FAIL_MODE_FLAG_CACHE_TTL
        == 30.0
    )


def test_failmode_reuses_content_filter_counter() -> None:
    """The metric is the SAME object (name + {phase,mode} labels cannot drift)."""
    assert failmode._fail_total is content_filter._content_filter_fail_total
    counter = content_filter._content_filter_fail_total
    assert counter._name == "waddleai_content_filter_fail"  # prometheus strips _total
    assert tuple(counter._labelnames) == ("phase", "mode")


def test_open_default_matches_source() -> None:
    """Both ContentFilter and the service config default the auditor fail mode to OPEN."""
    sig = inspect.signature(content_filter.ContentFilter.__init__)
    assert sig.parameters["auditor_fail_mode"].default == "open"
    assert ClassificationConfig().security_auditor_fail_mode == "open"
