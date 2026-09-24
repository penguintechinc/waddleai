"""Real end-to-end test: the FULL pipeline against a live Ollama host.

Every other e2e/contract/integration fixture in this repo sets
``WADDLEAI_STUB_UPSTREAM=1`` -- DispatchStage never makes a real network call,
and the proxy's own PyDAL schema (``shared/database/models.py``) creates
itself fresh (``migrate=True``) against an ephemeral sqlite file, so the
proxy's queries are always self-consistent with the schema it just created.
That hides any drift between this "legacy PyDAL schema" and the real,
Alembic-authoritative schema (``services/management/app/models_sqlalchemy.py``)
that every actual deployment (alpha/beta/gamma/prod) runs against.

This module drives the real, unstubbed path: real HTTP -> auth -> security/
PII -> smart routing -> DispatchStage -> a genuinely live Ollama host ->
response -> token metering -- against a throwaway PostgreSQL container
bootstrapped the *only* way a fresh cluster actually reaches migration head
in this repo (see ``_bootstrap_schema``'s docstring), not against sqlite.

Gating: only collected when ``WADDLEAI_LIVE_OLLAMA_URL`` is set (e.g.
``http://192.168.10.164:11434``); deselected entirely otherwise. Once set,
every fixture in this module raises ``pytest.fail`` (never
``pytest.skip``) on any setup problem -- a live host that turns out to be
unreachable, a missing ``docker`` binary, a schema bootstrap failure -- per
the "a gate that cannot fail is not a gate" standard (see
``tests/gpu_preflight.py``, whose ``require_live_model`` this module reuses
for its own preflight).

BUGS FOUND, gh-207 (previously hidden by every stub-upstream fixture).
Defects 1-3 are FIXED as of migration ``020_token_usage_api_key_id`` --
this module no longer applies any DB-level workaround (removed along with
the ``_apply_known_schema_drift_workarounds`` function that used to patch
around them) and exercises the real, corrected schema directly. Defect 4
remains open (tracked in gh-207, not fixed by this module):

1. ``shared/utils/token_manager.py`` reads/writes ``token_usage.api_key_id``
   and ``usage_cache.api_key_id``. The real (Alembic-authoritative) schema
   had no ``api_key_id`` column on either table -- only ``virtual_key_id``
   (``services/management/app/models_sqlalchemy.py``
   ``TokenUsage``/``UsageCache``), tied to the separate ``virtual_keys``
   table. Investigation confirmed ``virtual_key_id`` is genuinely vestigial
   on the proxy's request path: ``MeterStage``/``TokenBudgetStage``
   (``proxy/apps/proxy_server/pipeline/stages.py``) key off
   ``ctx.user.vkey_id``, an attribute ``shared.auth.rbac.UserContext`` --
   the only class ever assigned to ``ctx.user`` -- never sets, so that
   write path has always been inert; ``virtual_key_id`` is otherwise only
   exercised by the management service's own virtual-key CRUD, a separate
   admin feature. FIXED: migration ``020_token_usage_api_key_id`` adds
   ``api_key_id`` (nullable FK to ``api_keys.id``) to both tables;
   ``virtual_key_id`` is left in place, not dropped, per that migration's
   docstring.

2. ``content_filter_audit_log.timestamp`` was ``NOT NULL`` with only a
   Python-side SQLAlchemy ORM default (``default=datetime.utcnow`` in
   ``models_sqlalchemy.py``), which never applied when the row was inserted
   through PyDAL against ``get_db()``'s *reflected* Table object (default
   ``reflect=True``, ``shared/database/models.py``) -- a reflected table
   only carries the live catalog's server-side default, not a sibling ORM
   class's Python callable. ``shared/security/content_filter.py``'s
   ``_log_filter_event`` (~line 1652) carried a comment describing an
   *earlier* version of this same bug (a raw float epoch instead of a
   datetime) and "fixed" it by omitting the kwarg to rely on that default --
   which only actually worked against a PyDAL-native (sqlite,
   ``migrate=True``) schema, never against real Postgres. Every real
   audit-log insert failed ``NOT NULL``, so the compliance audit trail for
   every real PII/content-filter decision was silently never written in
   production (the failure was caught inside ``_log_filter_event`` itself
   and only logged, never raised). FIXED: migration
   ``020_token_usage_api_key_id`` adds a ``server_default`` (holds
   regardless of which layer inserts the row), and ``_log_filter_event``
   now also sets the value explicitly.

3. ``content_filter_audit_log.degraded`` was ALSO ``NOT NULL`` with no
   default at all, and ``_log_filter_event``'s ``insert()`` call never set
   it -- a second, independent ``NOT NULL`` violation stacked on bug 2.
   FIXED the same way as bug 2 (``server_default`` plus an explicit value
   from ``_log_filter_event``, now threaded from a real
   ``FilterResult.degraded`` signal set when the LLM auditor call fails
   and the pipeline falls back to the rule-based decision alone).

4. Migration ``001_baseline.py`` is a documented no-op ("all tables in this
   baseline were created by SQLAlchemy ``create_all()`` before Alembic was
   introduced ... run `alembic stamp 001_baseline` on existing databases").
   A genuinely fresh Postgres therefore cannot be bootstrapped by
   ``alembic upgrade head`` alone: migration ``002_add_provider_credentials``
   immediately fails with ``relation "ai_providers" does not exist``,
   because no migration ever creates that table -- only
   ``models_sqlalchemy.init_schema()``'s ``Base.metadata.create_all()``
   does. ``k8s/helm/waddleai/templates/migration-job.yaml`` runs bare
   ``alembic upgrade head`` with no ``init_schema()`` step first, which
   means that Job cannot bootstrap a truly fresh cluster either -- every
   existing alpha/beta/gamma deploy has only ever worked because a human
   ran ``create_all()`` by hand, once, long ago. This module's own
   ``_bootstrap_schema`` works around it by calling ``init_schema()``
   itself before ``alembic stamp head`` (not ``upgrade head``).

Two additional, previously-reported bugs (gh-130) were checked for and did
NOT reproduce on this checkout (``shared/auth/rbac.py:310`` already uses
penguin-dal's ``db(...).update(...)``, not the nonexistent
``Row.update_record()``; ``shared/utils/health_checks.py``'s
``self.db.executesql("SELECT 1")`` succeeded against this environment's
``penguin_dal`` -- ``/readyz``'s ``database`` check reported healthy with a
real query time). Recorded here rather than silently assumed fixed, per the
"trust but verify" standard.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import ProxyHandle, _free_port, _launch_proxy, _open_db
from tests.gpu_preflight import require_live_model

REPO = Path(__file__).resolve().parents[2]
MANAGEMENT_DIR = REPO / "services" / "management"

# Same Postgres digest .github/workflows/docker-build.yml pins for CI's own
# postgres service container -- reusing it (rather than a fresh, unpinned
# "postgres:15" tag) keeps this module's image identical to what CI already
# trusts and has cached, per the dependency-pinning standard.
_PG_IMAGE = (
    "postgres:15-bookworm@sha256:550245350d614cd36d1a8e90d76c6f6608172659312e53417becbbe722aa4179"
)

LIVE_OLLAMA_URL = os.environ.get("WADDLEAI_LIVE_OLLAMA_URL", "").rstrip("/")
_MODEL = os.environ.get("WADDLEAI_LIVE_MODEL", "gemma4:e4b")

pytestmark = pytest.mark.skipif(
    not LIVE_OLLAMA_URL,
    reason=(
        "set WADDLEAI_LIVE_OLLAMA_URL (e.g. http://192.168.10.164:11434) to run "
        "the real-upstream e2e suite -- deselected, not failed, when unset"
    ),
)


@dataclass(slots=True)
class PostgresHandle:
    """A running, throwaway PostgreSQL container: its DSN and container name."""

    database_url: str
    container_name: str


@dataclass(slots=True)
class RealOrgSeed:
    """A freshly seeded organization/user/api_key/connection_link row set."""

    org_id: int
    user_id: int
    api_key: str
    link_id: int


def _wait_for_postgres(container_name: str, deadline_s: float) -> None:
    """Poll ``pg_isready`` inside ``container_name`` until ready, or ``pytest.fail``."""
    deadline = time.time() + deadline_s
    last_output = ""
    while time.time() < deadline:
        check_argv = ["docker", "exec", container_name, "pg_isready", "-U", "waddleai"]
        # Fixed argv, no shell, this process's own container name -- same
        # pattern as tests/e2e/conftest.py's docker_redis fixture.
        result = subprocess.run(check_argv, capture_output=True, text=True, timeout=5)  # noqa: S603, S607
        if result.returncode == 0:
            return
        last_output = result.stdout + result.stderr
        time.sleep(1)
    pytest.fail(f"Postgres container {container_name!r} never became ready: {last_output}")


@pytest.fixture(scope="session")
def real_postgres() -> Any:
    """Start a throwaway, digest-pinned PostgreSQL 15 container for the whole session.

    ``WADDLEAI_LIVE_OLLAMA_URL`` being set means this whole module must
    prove the real path works -- so a missing ``docker`` binary or a
    container that never starts is a hard failure here (``pytest.fail``),
    never a skip.
    """
    if shutil.which("docker") is None:
        pytest.fail(
            "WADDLEAI_LIVE_OLLAMA_URL is set but docker is not available -- "
            "cannot start PostgreSQL for the real-upstream e2e suite"
        )

    port = _free_port()
    name = f"waddleai-realE2E-pg-{port}"
    password = "e2e-real-upstream-test-password"  # noqa: S105 -- throwaway local container, not a secret
    run_argv = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "-e",
        "POSTGRES_USER=waddleai",
        "-e",
        f"POSTGRES_PASSWORD={password}",
        "-e",
        "POSTGRES_DB=waddleai",
        "-p",
        f"{port}:5432",
        _PG_IMAGE,
    ]
    try:
        subprocess.run(run_argv, check=True, capture_output=True, timeout=60, text=True)  # noqa: S603
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"could not start the real-upstream e2e Postgres container: {exc.stderr}")

    database_url = f"postgresql://waddleai:{password}@127.0.0.1:{port}/waddleai"
    try:
        _wait_for_postgres(name, deadline_s=60.0)
        yield PostgresHandle(database_url=database_url, container_name=name)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)  # noqa: S603, S607


def _bootstrap_schema(database_url: str) -> None:
    """Bootstrap the REAL, Alembic-authoritative production schema onto ``database_url``.

    Mirrors the only sequence that actually reaches migration head on a
    genuinely fresh Postgres in this repo (see BUG 4 in the module
    docstring): ``models_sqlalchemy.init_schema()`` (``create_all``) first,
    then ``alembic stamp head`` -- NOT ``alembic upgrade head``, which
    fails immediately on an empty database because migration
    ``001_baseline`` is a documented no-op that assumes the baseline
    tables already exist.
    """
    sys.path.insert(0, str(MANAGEMENT_DIR))
    try:
        from app.models_sqlalchemy import init_schema  # noqa: PLC0415

        init_schema(database_url)
    finally:
        sys.path.remove(str(MANAGEMENT_DIR))

    from alembic.command import stamp  # noqa: PLC0415
    from alembic.config import Config  # noqa: PLC0415

    cfg = Config(str(MANAGEMENT_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MANAGEMENT_DIR / "alembic"))
    os.environ["DATABASE_URL"] = database_url  # alembic/env.py's get_url() reads this
    stamp(cfg, "head")


@pytest.fixture(scope="session")
def real_schema(real_postgres: PostgresHandle) -> PostgresHandle:
    """The Postgres container with the real schema bootstrapped onto it."""
    _bootstrap_schema(real_postgres.database_url)
    return real_postgres


def _seed_real_org(db_url: str, ollama_url: str) -> RealOrgSeed:
    """Seed one organization/user/api_key plus an enabled ``ollama`` ``connection_links`` row."""
    from passlib.hash import bcrypt  # noqa: PLC0415

    db = _open_db(db_url)
    now = datetime.utcnow()
    org_id = db.organizations.insert(
        name="e2e-real-upstream-org",
        description="real-upstream e2e (live Ollama, real Postgres schema)",
        token_quota_monthly=1_000_000,
        token_quota_daily=100_000,
        enabled=True,
        created_at=now,
    )
    user_id = db.users.insert(
        username="e2e-real-upstream-user",
        email="e2e-real-upstream@example.com",
        password_hash=bcrypt.hash("unused-not-a-real-login"),
        role="admin",
        organization_id=org_id,
        token_quota_monthly=1_000_000,
        token_quota_daily=100_000,
        enabled=True,
        created_at=now,
    )
    api_key_value = "wa-real-upstream-e2e-secretvalue1"
    db.api_keys.insert(
        key_id="e2e-real-upstream-key",
        key_hash=bcrypt.hash(api_key_value),
        user_id=user_id,
        organization_id=org_id,
        name="E2E real-upstream key",
        enabled=True,
        api_access_level="proxy_api",
        created_at=now,
    )
    # Sanctioned write path (finding #33): insert_connection_link encrypts any
    # api_key at rest. This ollama seed passes no key, so it is a no-op for
    # encryption -- but routing through the helper keeps every connection_links
    # creator on the one path that cannot accidentally persist plaintext.
    from shared.database.models import insert_connection_link  # noqa: PLC0415

    link_id = insert_connection_link(
        db,
        name="ollama-real-upstream",
        provider="ollama",
        endpoint_url=ollama_url,
        model_list=None,
        rate_limits=None,
        enabled=True,
    )
    db.commit()
    return RealOrgSeed(org_id=org_id, user_id=user_id, api_key=api_key_value, link_id=link_id)


@pytest.fixture(scope="session")
def real_org(real_schema: PostgresHandle) -> RealOrgSeed:
    """The seeded org/user/api_key/connection_link this module's tests authenticate as."""
    return _seed_real_org(real_schema.database_url, LIVE_OLLAMA_URL)


@pytest.fixture(scope="session")
def _live_model_preflight(real_org: RealOrgSeed) -> str:
    """Prove the live Ollama host is actually answering before any HTTP test runs.

    Reuses ``tests.gpu_preflight.require_live_model``, which ``pytest.fail``s
    (never skips) on any reachability/response problem -- exactly the "must
    FAIL, never skip" contract this whole module needs once
    ``WADDLEAI_LIVE_OLLAMA_URL`` is set. Depends on ``real_org`` only to
    guarantee ordering (schema + seeding before the first real HTTP call),
    not because it uses the seed data itself.
    """
    os.environ["OLLAMA_HOST"] = LIVE_OLLAMA_URL  # gpu_preflight.ollama_base_url() reads this
    reply = asyncio.run(require_live_model(_MODEL))
    print(f"[real-e2e preflight] 1 model probed ({_MODEL} @ {LIVE_OLLAMA_URL}): reply={reply!r}")
    return reply


@pytest.fixture(scope="session")
def real_proxy(
    real_schema: PostgresHandle,
    real_org: RealOrgSeed,
    _live_model_preflight: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Any:
    """The real proxy: no stub upstream, real Postgres schema, live Ollama, smart routing off."""
    port = _free_port()
    # _launch_proxy requires a db_dir argument to build its *default*
    # DATABASE_URL, but extra_env below overrides that default -- the
    # directory itself is never read.
    unused_db_dir = tmp_path_factory.mktemp("real_e2e_unused_db_dir")
    proc = _launch_proxy(
        port,
        unused_db_dir,
        extra_env={
            "WADDLEAI_STUB_UPSTREAM": "",
            "DATABASE_URL": real_schema.database_url,
            "OLLAMA_BASE_URL": LIVE_OLLAMA_URL,
        },
    )
    try:
        yield ProxyHandle(base_url=f"http://127.0.0.1:{port}", db_url=real_schema.database_url)
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="session")
def real_routing_proxy(
    real_schema: PostgresHandle,
    real_org: RealOrgSeed,
    _live_model_preflight: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Any:
    """A second real proxy process, isolated from ``real_proxy``, with smart routing ON.

    Separate process (not just a separate request) for the same reason
    ``tests/e2e/conftest.py``'s ``routing_proxy`` is split from
    ``proxy_process``: sharing one process between smart-routing-on and
    smart-routing-off flows risks the flag-on fallback path silently
    substituting a default model for unrelated requests.
    """
    port = _free_port()
    unused_db_dir = tmp_path_factory.mktemp("real_e2e_routing_unused_db_dir")
    proc = _launch_proxy(
        port,
        unused_db_dir,
        extra_env={
            "WADDLEAI_STUB_UPSTREAM": "",
            "DATABASE_URL": real_schema.database_url,
            "OLLAMA_BASE_URL": LIVE_OLLAMA_URL,
            "WADDLEAI_FLAG_SMART_ROUTING": "1",
        },
    )
    try:
        yield ProxyHandle(base_url=f"http://127.0.0.1:{port}", db_url=real_schema.database_url)
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_real_chat_completion_traverses_full_pipeline(
    real_proxy: ProxyHandle, real_org: RealOrgSeed, open_db: Any
) -> None:
    """A real HTTP chat completion crosses auth -> security -> dispatch -> live Ollama -> metering.

    Asserts the OpenAI-compatible response shape, that token usage is
    non-zero and internally consistent, that the request provably reached
    the live host (``/api/ps`` shows the model loaded there), and that
    metering was actually persisted to ``token_usage`` -- not just echoed
    in the response body.
    """
    prompt = "Reply with exactly this text and nothing else: waddleai-real-e2e-pong"
    resp = httpx.post(
        f"{real_proxy.base_url}/v1/chat/completions",
        headers={"Authorization": f"Bearer {real_org.api_key}"},
        json={"model": _MODEL, "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    content = body["choices"][0]["message"]["content"]
    assert content.strip(), "the live model returned empty content"
    assert body["model"] == _MODEL

    usage = body["usage"]
    assert usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] > 0
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]

    # Proves the request actually reached the live host, not a stub: the
    # model must now be loaded on the real Ollama server.
    ps = httpx.get(f"{LIVE_OLLAMA_URL}/api/ps", timeout=10).json()
    loaded_models = {m["name"] for m in ps.get("models", [])}
    assert _MODEL in loaded_models, f"live Ollama /api/ps never showed {_MODEL} loaded: {ps}"

    print(
        f"[real-e2e] real dispatch OK: model={body['model']!r} "
        f"prompt_tokens={usage['prompt_tokens']} completion_tokens={usage['completion_tokens']} "
        f"total_tokens={usage['total_tokens']} content={content!r}"
    )

    # Token accounting persisted for real (exercises the token_usage.api_key_id
    # workaround -- BUG 1 in the module docstring; without it this call 500s
    # before ever reaching this point).
    db = open_db(real_proxy.db_url)
    rows = db(db.token_usage.organization_id == real_org.org_id).select()
    assert len(rows) >= 1, "expected at least one persisted token_usage row"
    total_persisted_tokens = sum(
        (row.tokens_input_total or 0) + (row.tokens_output_total or 0) for row in rows
    )
    assert total_persisted_tokens > 0
    print(
        f"[real-e2e] token_usage rows examined={len(rows)} "
        f"persisted_tokens={total_persisted_tokens}"
    )


def test_pii_redaction_reaches_upstream_not_raw(
    real_proxy: ProxyHandle, real_org: RealOrgSeed, open_db: Any
) -> None:
    """A fake SSN and email are redacted before the live model ever sees them.

    Verifies via ``content_filter_audit_log.text_sample`` -- the exact
    string ``SecurityInStage`` writes back into ``ctx.messages`` before
    ``DispatchStage`` runs (``proxy/apps/proxy_server/pipeline/stages.py``,
    ``SecurityInStage.__call__``) -- rather than trusting the live model's
    reply, which is behavioral, not a code-path guarantee. Exercises the
    ``content_filter_audit_log`` workarounds (BUGS 2/3 in the module
    docstring): without them, this table never receives a row at all.
    """
    marker = "e2e-real-pii-check"
    ssn = "123-45-6789"
    email = "jane.doe@example.com"
    resp = httpx.post(
        f"{real_proxy.base_url}/v1/chat/completions",
        headers={"Authorization": f"Bearer {real_org.api_key}"},
        json={
            "model": _MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"My SSN is {ssn} and my email is {email} ({marker}). "
                        "Reply with exactly: received"
                    ),
                }
            ],
        },
        timeout=60,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    content = body["choices"][0]["message"]["content"]

    # The raw PII must never come back either -- a real model can only echo
    # what it actually received.
    assert ssn not in content
    assert email not in content

    db = open_db(real_proxy.db_url)
    matches = db(db.content_filter_audit_log.text_sample.contains(marker)).select(
        orderby=~db.content_filter_audit_log.id
    )
    assert len(matches) >= 1, "expected a content_filter_audit_log row for the PII-laden request"
    row = matches.first()
    assert row.phase == "input"
    assert row.action_taken == "redact"
    assert ssn not in row.text_sample
    assert email not in row.text_sample
    assert "[REDACTED:SSN]" in row.text_sample
    assert "[REDACTED:EMAIL]" in row.text_sample
    print(
        f"[real-e2e] PII audit rows examined={len(matches)} "
        f"upstream-bound text_sample={row.text_sample!r}"
    )


def test_routing_decision_is_recorded(
    real_routing_proxy: ProxyHandle, real_org: RealOrgSeed, open_db: Any
) -> None:
    """Smart routing picks a model and persists a ``routing_decision_traces`` row recording why.

    ``X-WaddleAI-Tool-Type: general`` bypasses the stage-2 classifier
    cascade (same rationale as ``tests/e2e/test_routing_redirect_e2e.py``)
    so this test isn't at the mercy of the live classifier's own model
    call for an unrelated concern.
    """
    db = open_db(real_routing_proxy.db_url)
    now = datetime.utcnow()
    db.model_configs.insert(
        model_name=_MODEL,
        preferred_providers=["ollama"],
        cost_per_token={"ollama": 0.0},
        max_tokens=4096,
        context_length=8192,
        capabilities=[],
        enabled=True,
        created_at=now,
    )
    db.model_assignments.insert(
        tool_type="general",
        model_name=_MODEL,
        enabled=True,
        scope="global",
        created_at=now,
    )
    db.commit()

    resp = httpx.post(
        f"{real_routing_proxy.base_url}/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {real_org.api_key}",
            "X-WaddleAI-Tool-Type": "general",
        },
        json={
            "model": _MODEL,
            "messages": [{"role": "user", "content": "e2e-real-routing-check"}],
        },
        timeout=60,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["model"] == _MODEL

    traces = db(db.routing_decision_traces.organization_id == real_org.org_id).select(
        orderby=~db.routing_decision_traces.id
    )
    assert len(traces) >= 1, "expected at least one routing_decision_traces row"
    trace = traces.first()
    assert trace.final_model == _MODEL
    assert trace.tool_type == "general"
    print(
        f"[real-e2e] routing_decision_traces examined={len(traces)} "
        f"final_model={trace.final_model!r} tool_type={trace.tool_type!r} "
        f"qualified_candidates={trace.qualified_candidates!r}"
    )
