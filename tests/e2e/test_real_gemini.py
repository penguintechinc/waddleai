"""Real end-to-end test: the FULL proxy pipeline + pooled quota against the live Gemini API.

Companion to ``test_real_upstream.py`` (live Ollama). Drives real HTTP -> auth (a ``wa-`` key
bound to a team) -> security/PII -> QuotaStage (reserve) -> DispatchStage -> the real
``GeminiConnector`` -> Gemini API -> metering -> quota reconcile, against a throwaway
PostgreSQL (real Alembic-authoritative schema) and a throwaway Valkey, with
``waddleai.pooled_quota`` ON so enforcement is ACTIVE.

Gating: collected only when ``GEMINI_API_KEY`` is set (else the module SKIPS, zero API calls).
With ``REQUIRE_GEMINI_TESTS=1`` a missing key (or any setup problem) FAILS, never skips --
"a gate that cannot fail is not a gate". The key is read from the environment only; it is stored
in the DB through ``insert_connection_link`` (encrypted at rest), blanked in the proxy's own
environment, and never printed or logged.

Credits: the test seeds a 1:1 conversion rate (1 credit == 1 LLM token) so the credits the quota
charges equal prompt + completion tokens exactly, which makes "charged the actual tokens"
directly assertable. Every counter assertion is a before/after DELTA, so test order is irrelevant.

COST GUARDRAILS: at most ``MAX_UPSTREAM_REQUESTS`` (6) calls can reach Gemini across the whole
module (hard-asserted); prompts are one-word replies with ``max_tokens=2048`` (a thinking
model spends output budget on reasoning first); the key's team quota
is small (50k credits reserved + 50k buffer; a thinking request is hundreds to low thousands).
Free-tier keys work.

REGRESSION GUARD: ``GeminiConnector`` used to raise TypeError on every call (``system_prompt``
kwarg) and, once fixed, meter tiktoken ESTIMATES. The ground-truth test
compares the proxy-reported prompt tokens with Gemini's own (free) ``count_tokens``; fails if
metering ever regresses to estimates.

Run live (key via env, never CLI args)::

    GEMINI_API_KEY=... REQUIRE_GEMINI_TESTS=1 [GEMINI_TEST_MODEL=gemini-3.8-flash] \
    [CREDENTIAL_ENCRYPTION_KEY=...] python3 -m pytest tests/e2e/test_real_gemini.py -v -s
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
import redis
from google import genai

from tests.e2e.conftest import ProxyHandle, _free_port, _launch_proxy, _open_db
from tests.e2e.test_real_upstream import (
    _PG_IMAGE,  # noqa: PLC2701 -- reuse the digest-pinned image CI already trusts
    PostgresHandle,
    _bootstrap_schema,
    _wait_for_postgres,
)
from tests.integration import valkey_harness

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
REQUIRE = os.environ.get("REQUIRE_GEMINI_TESTS") == "1"
MODEL = os.environ.get("GEMINI_TEST_MODEL", "gemini-3.8-flash")
MAX_UPSTREAM_REQUESTS = 6

pytestmark = pytest.mark.skipif(
    not GEMINI_API_KEY and not REQUIRE,
    reason=(
        "set GEMINI_API_KEY to run the real-Gemini e2e suite (REQUIRE_GEMINI_TESTS=1 "
        "makes a missing key a FAILURE) -- skipped, not failed, when unset"
    ),
)

_spent = {"upstream": 0}


def _spend(n: int = 1) -> None:
    """Account for ``n`` calls that can reach Gemini; fail hard past the module cap."""
    _spent["upstream"] += n
    assert _spent["upstream"] <= MAX_UPSTREAM_REQUESTS, (
        f"cost guardrail: >{MAX_UPSTREAM_REQUESTS} upstream requests in one run"
    )


@dataclass(slots=True)
class Seed:
    """Seeded ids/credentials for the module's two team-bound keys."""

    org_id: int
    user_id: int
    team_id: int
    key: str
    cycle: int
    broke_user_id: int
    broke_key: str


@pytest.fixture(scope="session")
def _gemini_key() -> str:
    """The provider key from the environment; FAIL (never skip) when required but absent."""
    if not GEMINI_API_KEY:
        pytest.fail("REQUIRE_GEMINI_TESTS=1 but GEMINI_API_KEY is not set")
    return GEMINI_API_KEY


@pytest.fixture(scope="session")
def gemini_pg(_gemini_key: str) -> Any:
    """Throwaway digest-pinned PostgreSQL 15 + real schema; fails (never skips) on trouble."""
    if shutil.which("docker") is None:
        pytest.fail("docker is not available -- cannot start PostgreSQL for the Gemini e2e")
    port = _free_port()
    name = f"waddleai-geminiE2E-pg-{port}"
    password = secrets.token_urlsafe(16)
    argv = [
        "docker", "run", "-d", "--rm", "--name", name,
        "-e", "POSTGRES_USER=waddleai", "-e", f"POSTGRES_PASSWORD={password}",
        "-e", "POSTGRES_DB=waddleai", "-p", f"127.0.0.1:{port}:5432", _PG_IMAGE,
    ]  # fmt: skip
    try:
        subprocess.run(argv, check=True, capture_output=True, timeout=120, text=True)  # noqa: S603
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"could not start the Gemini e2e Postgres container: {exc.stderr}")
    url = f"postgresql://waddleai:{password}@127.0.0.1:{port}/waddleai"
    try:
        _wait_for_postgres(name, deadline_s=60.0)
        _bootstrap_schema(url)
        yield PostgresHandle(database_url=url, container_name=name)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)  # noqa: S603, S607


@pytest.fixture(scope="session")
def valkey_url(_gemini_key: str) -> Any:
    """Throwaway Valkey; ``REQUIRE_VALKEY_TESTS`` is forced on so trouble fails, never skips."""
    os.environ[valkey_harness.REQUIRE_ENV] = "1"
    yield from (server.url for server in valkey_harness.start_valkey())


def _seed(db_url: str, api_key: str) -> Seed:
    """Seed org/teams/users/keys, the gemini provider, a 1:1 rate and the quota pools."""
    from passlib.hash import bcrypt  # noqa: PLC0415

    from services.management.app.services import quota_pools as qp  # noqa: PLC0415
    from shared.auth.rbac import mint_wa_credential  # noqa: PLC0415
    from shared.database.models import insert_connection_link  # noqa: PLC0415
    from shared.quota.cycle import utc_today  # noqa: PLC0415

    if not os.environ.get("CREDENTIAL_ENCRYPTION_KEY"):
        # Throwaway key (not a secret): lets the helper encrypt the provider key at rest; the
        # proxy subprocess inherits it so it can decrypt.
        os.environ["CREDENTIAL_ENCRYPTION_KEY"] = secrets.token_urlsafe(32)

    db = _open_db(db_url)
    now = datetime.utcnow()
    org = int(db.organizations.insert(name="e2e-gemini-org", enabled=True, created_at=now))
    teams = {
        k: int(db.teams.insert(organization_id=org, name=k, enabled=True))
        for k in ("e2e-gemini-team", "e2e-broke-team")
    }
    users: dict[str, int] = {}
    keys: dict[str, str] = {}
    for uname, team in (
        ("e2e-gemini-user", "e2e-gemini-team"),
        ("e2e-broke-user", "e2e-broke-team"),
    ):
        uid = int(
            db.users.insert(
                username=uname, email=f"{uname}@example.com", role="user",
                password_hash=bcrypt.hash(secrets.token_urlsafe(16)),
                organization_id=org, enabled=True, created_at=now,
            )
        )  # fmt: skip
        db.team_members.insert(
            team_id=teams[team], user_id=uid, organization_id=org, team_role="member"
        )
        key_id, key = mint_wa_credential()
        db.api_keys.insert(
            key_id=key_id, key_hash=bcrypt.hash(key), user_id=uid, organization_id=org,
            name=f"{uname}-key", enabled=True, api_access_level="proxy_api",
            team_id=teams[team], created_at=now,
        )  # fmt: skip
        users[uname], keys[uname] = uid, key
    # 1 credit == 1 LLM token, so the quota charge is directly input+output tokens.
    db.token_conversion_rates.insert(
        provider="gemini", model=MODEL, input_rate=1.0, output_rate=1.0,
        base_cost_per_waddleai_token=0.001, enabled=True,
    )  # fmt: skip
    # Sanctioned write path: encrypts api_key at rest. The key never appears in a log or argv.
    insert_connection_link(
        db, name="gemini-real-e2e", provider="gemini", endpoint_url="https://generativelanguage.googleapis.com",
        api_key=api_key, model_list=[MODEL], rate_limits=None, enabled=True,
    )  # fmt: skip
    # Pooled quota: org pool 1M; ok team 200k (50k guaranteed to the user + 50k buffer, sized for
    # thinking-model requests, which include reasoning tokens);
    # broke team 1 credit with a 0 guarantee and no buffer, so any request (>=2 credits) is denied.
    anchor = utc_today() - timedelta(days=1)  # -> cycle 0
    qp.set_org_pool(db, org, cycle_anchor_date=anchor, pool_credits=1_000_000, enabled=True)
    for tname, pool in (("e2e-gemini-team", 200_000), ("e2e-broke-team", 1)):
        team = db(db.teams.id == teams[tname]).select().first()
        qp.set_team_pool(db, org, team, pool)
    ok_team = db(db.teams.id == teams["e2e-gemini-team"]).select().first()
    qp.set_team_buffer(db, ok_team, 50_000)
    qp.set_user_reservation(db, ok_team, users["e2e-gemini-user"], 50_000, None)
    broke_team = db(db.teams.id == teams["e2e-broke-team"]).select().first()
    qp.set_user_reservation(db, broke_team, users["e2e-broke-user"], 0, None)
    db.commit()
    return Seed(
        org_id=org, user_id=users["e2e-gemini-user"], team_id=teams["e2e-gemini-team"],
        key=keys["e2e-gemini-user"], cycle=0, broke_user_id=users["e2e-broke-user"],
        broke_key=keys["e2e-broke-user"],
    )  # fmt: skip


@pytest.fixture(scope="session")
def seed(gemini_pg: PostgresHandle, _gemini_key: str) -> Seed:
    """The seeded world (must run before the proxy boots: rates/connectors load at startup)."""
    return _seed(gemini_pg.database_url, _gemini_key)


@pytest.fixture(scope="session")
def proxy(
    gemini_pg: PostgresHandle,
    valkey_url: str,
    seed: Seed,
    tmp_path_factory: pytest.TempPathFactory,
) -> Any:
    """The real proxy: no stub upstream, real Postgres + Valkey, pooled-quota flag ON."""
    port = _free_port()
    proc = _launch_proxy(
        port,
        tmp_path_factory.mktemp("gemini_e2e_unused_db_dir"),
        extra_env={
            "WADDLEAI_STUB_UPSTREAM": "",
            "DATABASE_URL": gemini_pg.database_url,
            "REDIS_URL": valkey_url,
            "WADDLEAI_FLAG_POOLED_QUOTA": "1",
            "GEMINI_API_KEY": "",  # prove the credential comes from the encrypted DB row
        },
    )
    try:
        yield ProxyHandle(base_url=f"http://127.0.0.1:{port}", db_url=gemini_pg.database_url)
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="session")
def valkey_client(valkey_url: str) -> Any:
    """Sync Valkey client (decoded) for reading the ``wa:quota:*`` counters."""
    client = redis.Redis.from_url(valkey_url, decode_responses=True)
    yield client
    client.close()


def _counter(valkey: Any, key: str) -> int:
    """Integer value of a Valkey counter (0 when absent)."""
    return int(valkey.get(key) or 0)


def _post(proxy: ProxyHandle, key: str, prompt: str) -> httpx.Response:
    """POST one small OpenAI-shape ``/v1/chat/completions`` request as ``key``."""
    return httpx.post(
        f"{proxy.base_url}/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": MODEL,
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=90,
    )


def _charged_request(
    proxy: ProxyHandle, seed: Seed, valkey: Any, prompt: str
) -> tuple[dict, int, int]:
    """Send one real request; return ``(body, tokens_reported, user_counter_delta)``."""
    from shared.quota import keys  # noqa: PLC0415

    before = _counter(valkey, keys.user_counter(seed.user_id, seed.cycle))
    _spend()
    resp = _post(proxy, seed.key, prompt)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    usage = body["usage"]
    tokens = int(usage["prompt_tokens"]) + int(usage["completion_tokens"])
    delta = _counter(valkey, keys.user_counter(seed.user_id, seed.cycle)) - before
    return body, tokens, delta


def test_real_chat_success_and_quota_charged_actual_tokens(
    proxy: ProxyHandle, seed: Seed, valkey_client: Any, open_db: Any
) -> None:
    """A real Gemini reply succeeds and quota reserve->reconcile charged prompt+completion tokens.

    Enforcement is provably active: with the flag off or QuotaStage a no-op the user counter
    would stay 0. Reserve admits ~3x the prompt estimate; reconcile settles to the ACTUAL
    credits, which under the seeded 1:1 rate equal the reported ``prompt+completion`` tokens. The
    Valkey counter, the durable ledger row and ``token_usage.waddleai_tokens`` must all agree.
    """
    from shared.quota import keys  # noqa: PLC0415
    from shared.quota.ledger import QuotaLedger  # noqa: PLC0415

    body, tokens, delta = _charged_request(proxy, seed, valkey_client, "Reply with one word")
    assert body["choices"][0]["message"]["content"].strip(), "Gemini returned empty text"
    assert body["model"] == MODEL
    assert tokens > 0
    assert delta == tokens, f"quota counter moved {delta}, reported tokens {tokens}"
    assert _counter(valkey_client, keys.buffer_counter(seed.team_id, seed.cycle)) == 0

    db = open_db(proxy.db_url)
    ledger = {
        (e.scope_type, e.scope_id): e.credits
        for e in QuotaLedger(db).usage(seed.org_id, seed.cycle)
    }
    assert ledger.get(("user", seed.user_id)) == _counter(
        valkey_client, keys.user_counter(seed.user_id, seed.cycle)
    )
    rows = db(db.token_usage.user_id == seed.user_id).select()
    assert sum(int(r.waddleai_tokens or 0) for r in rows) == ledger[("user", seed.user_id)]
    print(f"[gemini-e2e] tokens={tokens} counter_delta={delta} ledger={ledger}")


def test_second_request_accumulates_actual_tokens(
    proxy: ProxyHandle, seed: Seed, valkey_client: Any
) -> None:
    """A second real request adds exactly its own reported tokens (no double charge/leak)."""
    body, tokens, delta = _charged_request(proxy, seed, valkey_client, "Reply with the word: ok")
    assert body["choices"][0]["message"]["content"].strip()
    assert delta == tokens, f"quota counter moved {delta}, reported tokens {tokens}"


def test_pii_is_redacted_before_egress(proxy: ProxyHandle, seed: Seed, open_db: Any) -> None:
    """A fake SSN/email are scrubbed by the security stage before the prompt reaches Gemini.

    Verified through ``content_filter_audit_log.text_sample`` -- the exact text
    ``SecurityInStage`` writes back into ``ctx.messages`` before ``DispatchStage`` runs --
    plus the reply never echoing the raw values.
    """
    marker, ssn, email = "e2e-gemini-pii-check", "123-45-6789", "jane.doe@example.com"
    _spend()
    resp = _post(
        proxy,
        seed.key,
        f"My SSN is {ssn} and my email is {email} ({marker}). Reply with exactly: received",
    )
    assert resp.status_code == 200, resp.text
    reply = resp.json()["choices"][0]["message"]["content"]
    assert ssn not in reply
    assert email not in reply
    db = open_db(proxy.db_url)
    matches = db(db.content_filter_audit_log.text_sample.contains(marker)).select(
        orderby=~db.content_filter_audit_log.id
    )
    assert len(matches) >= 1, "expected a content_filter_audit_log row for the PII request"
    row = matches.first()
    assert row.phase == "input"
    assert row.action_taken == "redact"
    assert ssn not in row.text_sample
    assert email not in row.text_sample
    assert "[REDACTED:SSN]" in row.text_sample
    assert "[REDACTED:EMAIL]" in row.text_sample
    print(f"[gemini-e2e] PII audit rows={len(matches)} egress text_sample={row.text_sample!r}")


def test_exhausted_team_is_429_without_any_upstream_call(
    proxy: ProxyHandle, seed: Seed, valkey_client: Any, open_db: Any
) -> None:
    """A key whose team has no credits is denied 429 by QuotaStage before dispatch (zero cost)."""
    from shared.quota import keys  # noqa: PLC0415

    resp = _post(proxy, seed.broke_key, "Reply with one word")  # not _spend(): never reaches Gemini
    assert resp.status_code == 429, resp.text
    assert "quota" in resp.text.lower()
    assert _counter(valkey_client, keys.user_counter(seed.broke_user_id, seed.cycle)) == 0
    db = open_db(proxy.db_url)
    assert db(db.token_usage.user_id == seed.broke_user_id).count() == 0


def test_reported_usage_matches_gemini_ground_truth(
    proxy: ProxyHandle, seed: Seed, valkey_client: Any, _gemini_key: str
) -> None:
    """Proxy-reported ``prompt_tokens`` must equal Gemini's own count for the same prompt.

    Ground truth is Gemini's free ``count_tokens`` endpoint called via the SDK directly (the
    connector's own ``count_tokens`` silently falls back to tiktoken, so it cannot be an oracle).
    Fails if ``GeminiConnector`` regresses to tiktoken estimates instead of ``usage_metadata``.
    """
    prompt = "Reply with one word"
    body, _tokens, _delta = _charged_request(proxy, seed, valkey_client, prompt)
    real = genai.Client(api_key=_gemini_key).models.count_tokens(
        model=f"models/{MODEL}", contents=prompt
    )
    reported = int(body["usage"]["prompt_tokens"])
    assert reported == real.total_tokens, (
        f"proxy reported prompt_tokens={reported}, Gemini counted {real.total_tokens} "
        "(connector is metering estimates, not actual usage)"
    )
