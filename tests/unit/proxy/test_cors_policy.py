"""Regression tests for the 2026-09-14 proxy CORS finding.

Finding 3 (MEDIUM) -- ``main.py``'s ``add_cors_headers`` set
``Access-Control-Allow-Origin: *``, ``Access-Control-Allow-Headers: *`` and
``Access-Control-Allow-Credentials: true`` on *every* response, pairing an
anonymous wildcard with credentials. These tests pin the replacement policy:
an env-driven allowlist, an echoed origin, and the invariant that a literal
``*`` and ``Allow-Credentials: true`` never appear on the same response.

regression: audit-2026-09-14
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

_DB_DIR = tempfile.mkdtemp(prefix="waddleai-proxy-cors-test-")
os.environ.setdefault("WADDLEAI_STUB_UPSTREAM", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB_DIR}/test.db")
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("RELEASE_MODE", "false")
os.environ.setdefault("CACHE_HOST", "")

import pytest  # noqa: E402 -- env vars above must be set before importing main.py
from quart import Response  # noqa: E402

from proxy.apps.proxy_server.main import (  # noqa: E402
    CORS_ALLOW_HEADERS_ENV_VAR,
    CORS_ORIGINS_ENV_VAR,
    CORSPolicy,
    CORSPreflightMiddleware,
    add_cors_headers,
    app,
    cors_headers,
    load_cors_policy,
)

pytestmark = pytest.mark.security

ALLOWED_ORIGIN = "https://console.penguintech.cloud"
OTHER_ORIGIN = "https://evil.example.com"

#: Every policy shape the proxy can be configured into, including the
#: explicitly-opted-into wildcard -- the invariant below must hold for all.
_POLICIES: list[CORSPolicy] = [
    CORSPolicy(),
    CORSPolicy(origins=(ALLOWED_ORIGIN,)),
    CORSPolicy(origins=(ALLOWED_ORIGIN, "https://app.penguintech.cloud")),
    CORSPolicy(origins=("*",)),
    CORSPolicy(origins=("*", ALLOWED_ORIGIN)),
]

_ORIGINS: list[str] = ["", ALLOWED_ORIGIN, OTHER_ORIGIN, "null"]


class _EchoApp:
    """Minimal ASGI app recording whether CORSPreflightMiddleware forwarded to it."""

    def __init__(self) -> None:
        """Start with no forwarded calls recorded."""
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        """Record the forwarded scope and emit a trivial 200."""
        self.calls.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})


async def _drive_preflight(
    policy: CORSPolicy, headers: list[tuple[bytes, bytes]], method: str = "OPTIONS"
) -> tuple[_EchoApp, list[dict[str, Any]]]:
    """Run CORSPreflightMiddleware over one request, returning the app and sent messages."""
    inner = _EchoApp()
    middleware = CORSPreflightMiddleware(inner, policy)
    sent: list[dict[str, Any]] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    await middleware({"type": "http", "method": method, "headers": headers}, receive, send)
    return inner, sent


def _header_map(sent: list[dict[str, Any]]) -> dict[str, str]:
    """Extract the response-start headers from a list of ASGI messages."""
    start = next(msg for msg in sent if msg["type"] == "http.response.start")
    return {key.decode().lower(): value.decode() for key, value in start["headers"]}


class TestWildcardNeverPairsWithCredentials:
    """The invariant the old handler broke, over every policy/origin combination.

    regression: audit-2026-09-14
    """

    @pytest.mark.parametrize("policy", _POLICIES)
    @pytest.mark.parametrize("origin", _ORIGINS)
    @pytest.mark.parametrize("preflight", [False, True])
    async def test_computed_headers_never_pair_star_with_credentials(
        self, policy: CORSPolicy, origin: str, preflight: bool
    ) -> None:
        """No configuration yields `*` and Allow-Credentials on the same response."""
        headers = cors_headers(origin, policy, preflight=preflight)

        if headers.get("Access-Control-Allow-Origin") == "*":
            assert "Access-Control-Allow-Credentials" not in headers
        if headers.get("Access-Control-Allow-Credentials") == "true":
            assert headers["Access-Control-Allow-Origin"] != "*"
        # A wildcard Allow-Headers is equally invalid with credentials.
        assert headers.get("Access-Control-Allow-Headers") != "*"

    @pytest.mark.parametrize("policy", _POLICIES)
    @pytest.mark.parametrize("origin", _ORIGINS)
    async def test_live_response_never_pairs_star_with_credentials(
        self, policy: CORSPolicy, origin: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real after_request handler never emits the forbidden combination."""
        monkeypatch.setattr("proxy.apps.proxy_server.main._cors_policy", policy)

        request_headers = {"Origin": origin} if origin else {}
        async with app.test_request_context("/v1/models", method="GET", headers=request_headers):
            response = await add_cors_headers(Response("ok"))

        allow_origin = response.headers.get("Access-Control-Allow-Origin")
        allow_credentials = response.headers.get("Access-Control-Allow-Credentials")
        assert not (allow_origin == "*" and allow_credentials == "true")
        assert response.headers.get("Access-Control-Allow-Headers") != "*"

    @pytest.mark.parametrize("policy", _POLICIES)
    @pytest.mark.parametrize("origin", _ORIGINS)
    async def test_preflight_response_never_pairs_star_with_credentials(
        self, policy: CORSPolicy, origin: str
    ) -> None:
        """The ASGI preflight responder obeys the same invariant."""
        headers: list[tuple[bytes, bytes]] = [(b"access-control-request-method", b"POST")]
        if origin:
            headers.append((b"origin", origin.encode()))

        _, sent = await _drive_preflight(policy, headers)
        emitted = _header_map(sent)

        assert not (
            emitted.get("access-control-allow-origin") == "*"
            and emitted.get("access-control-allow-credentials") == "true"
        )
        assert emitted.get("access-control-allow-headers") != "*"


class TestAllowlistBehaviour:
    """Origins are echoed from an allowlist; the default denies everything.

    regression: audit-2026-09-14
    """

    def test_default_policy_is_not_wildcard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unset CORS_ORIGINS yields an empty allowlist, never `*`."""
        monkeypatch.delenv(CORS_ORIGINS_ENV_VAR, raising=False)
        policy = load_cors_policy({})

        assert policy.origins == ()
        assert policy.wildcard is False

    def test_default_allow_headers_cover_every_header_the_proxy_reads(self) -> None:
        """The header allowlist is explicit and complete -- a wildcard is never used."""
        policy = load_cors_policy({CORS_ORIGINS_ENV_VAR: ALLOWED_ORIGIN})
        advertised = {header.lower() for header in policy.headers}

        assert "*" not in policy.headers
        for header in (
            "authorization",
            "content-type",
            "x-api-key",
            "x-preferred-model",
            "x-session-id",
            "x-waddleai-escalate",
            "x-waddleai-session",
            "x-waddleai-tool-type",
        ):
            assert header in advertised

    def test_allow_headers_can_be_overridden_from_the_environment(self) -> None:
        """CORS_ALLOW_HEADERS replaces the default allowlist for unusual deployments."""
        policy = load_cors_policy(
            {CORS_ORIGINS_ENV_VAR: ALLOWED_ORIGIN, CORS_ALLOW_HEADERS_ENV_VAR: "A, B"}
        )

        assert policy.headers == ("A", "B")

    def test_env_var_is_parsed_as_a_comma_separated_allowlist(self) -> None:
        """CORS_ORIGINS follows the management service's comma-separated convention."""
        policy = load_cors_policy(
            {CORS_ORIGINS_ENV_VAR: f" {ALLOWED_ORIGIN} , https://b.example , "}
        )

        assert policy.origins == (ALLOWED_ORIGIN, "https://b.example")

    async def test_no_cors_headers_when_no_allowlist_configured(self) -> None:
        """With no allowlist the response carries no CORS headers at all."""
        policy = CORSPolicy()

        assert cors_headers(ALLOWED_ORIGIN, policy) == {}

    async def test_allowlisted_origin_is_echoed_with_credentials(self) -> None:
        """An allowlisted origin is echoed verbatim and may carry credentials."""
        policy = CORSPolicy(origins=(ALLOWED_ORIGIN,))
        headers = cors_headers(ALLOWED_ORIGIN, policy)

        assert headers["Access-Control-Allow-Origin"] == ALLOWED_ORIGIN
        assert headers["Access-Control-Allow-Credentials"] == "true"

    async def test_unlisted_origin_gets_nothing(self) -> None:
        """An origin outside the allowlist receives no CORS headers."""
        policy = CORSPolicy(origins=(ALLOWED_ORIGIN,))

        assert cors_headers(OTHER_ORIGIN, policy) == {}

    async def test_response_varies_on_origin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Vary: Origin is set so caches never serve one origin's headers to another."""
        monkeypatch.setattr(
            "proxy.apps.proxy_server.main._cors_policy", CORSPolicy(origins=(ALLOWED_ORIGIN,))
        )

        async with app.test_request_context(
            "/v1/models", method="GET", headers={"Origin": OTHER_ORIGIN}
        ):
            response = await add_cors_headers(Response("ok"))

        assert "origin" in response.headers.get("Vary", "").lower()
        assert "Access-Control-Allow-Origin" not in response.headers


class TestPreflightHandling:
    """Preflights are answered ahead of the auth chain, with the full header set.

    regression: audit-2026-09-14
    """

    async def test_allowlisted_preflight_is_answered_204_without_reaching_the_app(self) -> None:
        """A valid preflight short-circuits: 204 + CORS headers, app never invoked."""
        policy = CORSPolicy(origins=(ALLOWED_ORIGIN,))
        inner, sent = await _drive_preflight(
            policy,
            [
                (b"origin", ALLOWED_ORIGIN.encode()),
                (b"access-control-request-method", b"POST"),
            ],
        )
        emitted = _header_map(sent)

        assert inner.calls == []
        assert next(m for m in sent if m["type"] == "http.response.start")["status"] == 204
        assert emitted["access-control-allow-origin"] == ALLOWED_ORIGIN
        assert emitted["access-control-allow-credentials"] == "true"
        assert "POST" in emitted["access-control-allow-methods"]
        assert "Authorization" in emitted["access-control-allow-headers"]
        assert emitted["access-control-max-age"] == "600"

    async def test_unlisted_preflight_is_refused_without_reaching_the_app(self) -> None:
        """A preflight from an unlisted origin is refused, not forwarded to auth."""
        policy = CORSPolicy(origins=(ALLOWED_ORIGIN,))
        inner, sent = await _drive_preflight(
            policy,
            [(b"origin", OTHER_ORIGIN.encode()), (b"access-control-request-method", b"POST")],
        )
        emitted = _header_map(sent)

        assert inner.calls == []
        assert next(m for m in sent if m["type"] == "http.response.start")["status"] == 403
        assert "access-control-allow-origin" not in emitted

    async def test_plain_options_request_is_forwarded(self) -> None:
        """A non-preflight OPTIONS request still belongs to the application."""
        policy = CORSPolicy(origins=(ALLOWED_ORIGIN,))
        inner, _ = await _drive_preflight(policy, [(b"origin", ALLOWED_ORIGIN.encode())])

        assert len(inner.calls) == 1

    async def test_non_options_request_is_forwarded(self) -> None:
        """Ordinary requests pass straight through the preflight middleware."""
        policy = CORSPolicy(origins=(ALLOWED_ORIGIN,))
        inner, _ = await _drive_preflight(
            policy, [(b"origin", ALLOWED_ORIGIN.encode())], method="POST"
        )

        assert len(inner.calls) == 1


class TestWildcardSourceIsGone:
    """The literal wildcard trio must not reappear in main.py.

    regression: audit-2026-09-14
    """

    def test_after_request_handler_has_no_hardcoded_wildcards(self) -> None:
        """The old unconditional wildcard assignments are absent from the source."""
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[3] / "proxy" / "apps" / "proxy_server" / "main.py"
        ).read_text()

        assert 'response.headers["Access-Control-Allow-Origin"] = "*"' not in source
        assert 'response.headers["Access-Control-Allow-Headers"] = "*"' not in source
        assert 'response.headers["Access-Control-Allow-Credentials"] = "true"' not in source
