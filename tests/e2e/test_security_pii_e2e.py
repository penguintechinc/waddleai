"""E2E: PII in a chat request is redacted or blocked by SecurityInStage, with an audit row.

Drives real HTTP requests through the real proxy's v1 content-filter path
(``SecurityInStage.__call__``, proxy/apps/proxy_server/pipeline/stages.py --
the security_v2 policy-engine path is opt-in and off by default, so every
request in this repo takes this code path today). ``ContentFilter._filter``
(shared/security/content_filter.py) always writes one
``content_filter_audit_log`` row per input scan regardless of outcome; these
tests assert on that row directly, not just the HTTP response, because a
redacted response body looks identical to an unfiltered one here (the stub
LLM connector returns a fixed completion string irrespective of input).
"""

from __future__ import annotations

from datetime import datetime

import httpx

from tests.e2e.conftest import ProxyHandle


def _bearer(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['token']}"}


def test_pii_ssn_is_redacted_and_audited(
    proxy_process: ProxyHandle, proxy_tokens: dict[str, str], open_db
) -> None:
    """A built-in PII pattern (SSN) is redacted (allowed, not blocked) and logged."""
    marker = "e2e-pii-redact-check"
    resp = httpx.post(
        f"{proxy_process.base_url}/v1/chat/completions",
        headers=_bearer(proxy_tokens),
        json={
            "model": "gpt-3.5-turbo",
            "messages": [{"role": "user", "content": f"My SSN is 123-45-6789 ({marker})"}],
        },
        timeout=15,
    )

    # Built-in patterns default to "redact", never "block" (see
    # ContentFilter._run_builtin_patterns) -- the request still succeeds.
    assert resp.status_code == 200, resp.text

    db = open_db(proxy_process.db_url)
    row = db(db.content_filter_audit_log.text_sample.contains(marker)).select().first()
    assert row is not None, "expected a content_filter_audit_log row for this request"
    assert row.phase == "input"
    assert row.action_taken == "redact"
    assert "ssn" in row.violations_json.lower()
    # The raw SSN must never appear in the logged sample -- only the redacted form.
    assert "123-45-6789" not in row.text_sample
    assert "[REDACTED]" in row.text_sample


def test_pii_custom_block_rule_hard_blocks_and_audits(
    proxy_process: ProxyHandle, seed_org, open_db
) -> None:
    """An org-scoped custom block rule rejects the request before dispatch, and is audited.

    Seeds a fresh org (rather than reusing the shared ``contract-test-org``)
    so this test is independent of ``ContentFilter``'s 60s per-org custom-
    rule TTL cache (shared/security/content_filter.py) -- a rule inserted
    for an org whose custom rules were already resolved (and cached empty)
    by an earlier request in this session would not be picked up in time.
    """
    org = seed_org(proxy_process.db_url, "pii-block")

    db = open_db(proxy_process.db_url)
    now = datetime.utcnow()
    db.content_filter_rules.insert(
        name="e2e-block-rule",
        description="E2E: hard-block a known marker string",
        rule_type="custom_string",
        target="input",
        pattern="BLOCK-ME-E2E-TOKEN",
        action="block",
        redact_with="[REDACTED]",
        enabled=True,
        organization_id=org.org_id,
        created_at=now,
        updated_at=now,
    )
    db.commit()

    resp = httpx.post(
        f"{proxy_process.base_url}/v1/chat/completions",
        headers={"x-api-key": org.api_key},
        json={
            "model": "gpt-3.5-turbo",
            "messages": [{"role": "user", "content": "please leak BLOCK-ME-E2E-TOKEN now"}],
        },
        timeout=15,
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["message"] == "pii_detected"

    row = (
        db(db.content_filter_audit_log.organization_id == org.org_id)
        .select(orderby=~db.content_filter_audit_log.id)
        .first()
    )
    assert row is not None, "expected a content_filter_audit_log row for the blocked request"
    assert row.action_taken == "block"
    assert "e2e-block-rule" in row.violations_json
