"""Tests for ``penguincode_cli.client.waddleai_auth`` -- CLI WaddleAI JWT acquisition (F4).

TDD: written before ``penguincode_cli/client/waddleai_auth.py`` exists; must fail
with an ImportError/ModuleNotFoundError until the module is implemented. No live
network -- ``httpx.MockTransport`` stands in for the WaddleAI management REST API,
and a real (but test-only) RSA keypair stands in for local-dev signing.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest

from penguincode_cli.client.waddleai_auth import (
    WaddleAIAuthConfig,
    WaddleAIAuthError,
    WaddleAICredentialsError,
    WaddleAIDevModeRefusedError,
    WaddleAITokenInvalidError,
    WaddleAITokenProvider,
    WaddleAITokenStore,
)

ISSUER = "https://waddleai.test"
AUDIENCE = "waddleai-api-test"


def _make_config(tmp_path: Path, **overrides: Any) -> WaddleAIAuthConfig:
    defaults: dict[str, Any] = {
        "issuer_url": ISSUER,
        "username": "alice",
        "password": "hunter2",  # noqa: S106 -- test fixture credential, not a real secret
        "audience": AUDIENCE,
        "token_path": str(tmp_path / "waddleai_token.json"),
        "dev_mode": False,
        "request_timeout": 5.0,
        "refresh_leeway_seconds": 60,
    }
    defaults.update(overrides)
    return WaddleAIAuthConfig(**defaults)


def _login_response_token(*, exp_delta: int = 3600, sub: str = "user-1") -> str:
    now = int(time.time())
    claims = {
        "sub": sub,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + exp_delta,
        "tenant": "tenant-abc",
        "teams": ["team-1"],
        "scope": ["widgets:read"],
    }
    # Client-side validation never checks the signature, so an ad-hoc HMAC
    # secret is sufficient here -- no need to stand up a real RSA keypair for
    # every server-response fixture.
    return jwt.encode(claims, "test-signing-secret-at-least-32-bytes-long", algorithm="HS256")


def _client_factory_for(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[[], httpx.AsyncClient]:
    def factory() -> httpx.AsyncClient:
        transport = httpx.MockTransport(handler)
        return httpx.AsyncClient(transport=transport)

    return factory


class TestWaddleAITokenStore:
    def test_round_trip_and_permissions(self, tmp_path: Path) -> None:
        from penguincode_cli.client.waddleai_auth import _CachedToken

        store = WaddleAITokenStore(str(tmp_path / "sub" / "token.json"))
        assert store.load() is None

        token = _CachedToken(
            access_token="tok-1", expires_at=123.0, issuer=ISSUER, audience=AUDIENCE, is_dev=False
        )
        store.save(token)

        loaded = store.load()
        assert loaded is not None
        assert loaded.access_token == "tok-1"
        assert loaded.issuer == ISSUER

        mode = (tmp_path / "sub" / "token.json").stat().st_mode
        assert mode & 0o077 == 0, "token cache file must not be group/world readable"

    def test_clear_removes_file(self, tmp_path: Path) -> None:
        from penguincode_cli.client.waddleai_auth import _CachedToken

        path = tmp_path / "token.json"
        store = WaddleAITokenStore(str(path))
        store.save(
            _CachedToken(
                access_token="tok", expires_at=1.0, issuer=ISSUER, audience=AUDIENCE, is_dev=False
            )
        )
        assert path.exists()
        store.clear()
        assert not path.exists()

    def test_corrupt_cache_treated_as_absent(self, tmp_path: Path) -> None:
        path = tmp_path / "token.json"
        path.write_text("not json", encoding="utf-8")
        store = WaddleAITokenStore(str(path))
        assert store.load() is None


class TestLogin:
    @pytest.mark.asyncio
    async def test_login_acquires_and_caches_token(self, tmp_path: Path) -> None:
        issued = _login_response_token()
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            assert request.url.path == "/api/v1/auth/login"
            body = json.loads(request.content)
            assert body == {"username": "alice", "password": "hunter2"}
            return httpx.Response(
                200, json={"access_token": issued, "token_type": "bearer", "expires_in": 3600}
            )

        provider = WaddleAITokenProvider(
            _make_config(tmp_path), client_factory=_client_factory_for(handler)
        )
        token = await provider.get_access_token()

        assert token == issued
        assert len(calls) == 1
        cached = provider._store.load()
        assert cached is not None
        assert cached.access_token == issued
        assert cached.is_dev is False

    @pytest.mark.asyncio
    async def test_cached_token_reused_without_network_call(self, tmp_path: Path) -> None:
        issued = _login_response_token(exp_delta=3600)
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"access_token": issued, "expires_in": 3600})

        provider = WaddleAITokenProvider(
            _make_config(tmp_path), client_factory=_client_factory_for(handler)
        )
        first = await provider.get_access_token()
        second = await provider.get_access_token()

        assert first == second
        assert calls == 1, "second call must reuse the cache, not hit the network"

    @pytest.mark.asyncio
    async def test_login_rejected_raises(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "Invalid credentials"})

        provider = WaddleAITokenProvider(
            _make_config(tmp_path), client_factory=_client_factory_for(handler)
        )
        with pytest.raises(WaddleAIAuthError):
            await provider.get_access_token()

    @pytest.mark.asyncio
    async def test_missing_credentials_raises_before_any_request(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not make a network call with no credentials")

        provider = WaddleAITokenProvider(
            _make_config(tmp_path, username=None, password=None),
            client_factory=_client_factory_for(handler),
        )
        with pytest.raises(WaddleAICredentialsError):
            await provider.get_access_token()


class TestRefresh:
    @pytest.mark.asyncio
    async def test_near_expiry_token_is_refreshed(self, tmp_path: Path) -> None:
        from penguincode_cli.client.waddleai_auth import _CachedToken

        old_token = _login_response_token(exp_delta=30)  # inside the 60s leeway
        new_token = _login_response_token(exp_delta=3600, sub="user-1-refreshed")
        seen_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_paths.append(request.url.path)
            assert request.url.path == "/api/v1/auth/refresh"
            assert request.headers["authorization"] == f"Bearer {old_token}"
            return httpx.Response(200, json={"access_token": new_token, "expires_in": 3600})

        config = _make_config(tmp_path)
        provider = WaddleAITokenProvider(config, client_factory=_client_factory_for(handler))
        provider._store.save(
            _CachedToken(
                access_token=old_token,
                expires_at=time.time() + 30,
                issuer=ISSUER,
                audience=AUDIENCE,
                is_dev=False,
            )
        )

        token = await provider.get_access_token()

        assert token == new_token
        assert seen_paths == ["/api/v1/auth/refresh"]

    @pytest.mark.asyncio
    async def test_refresh_rejected_falls_back_to_login(self, tmp_path: Path) -> None:
        from penguincode_cli.client.waddleai_auth import _CachedToken

        old_token = _login_response_token(exp_delta=30)
        new_token = _login_response_token(exp_delta=3600)
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/api/v1/auth/refresh":
                return httpx.Response(401, json={"error": "Invalid or expired token"})
            assert request.url.path == "/api/v1/auth/login"
            return httpx.Response(200, json={"access_token": new_token, "expires_in": 3600})

        config = _make_config(tmp_path)
        provider = WaddleAITokenProvider(config, client_factory=_client_factory_for(handler))
        provider._store.save(
            _CachedToken(
                access_token=old_token,
                expires_at=time.time() + 30,
                issuer=ISSUER,
                audience=AUDIENCE,
                is_dev=False,
            )
        )

        token = await provider.get_access_token()

        assert token == new_token
        assert paths == ["/api/v1/auth/refresh", "/api/v1/auth/login"]

    @pytest.mark.asyncio
    async def test_fully_expired_cache_skips_refresh_goes_straight_to_login(
        self, tmp_path: Path
    ) -> None:
        from penguincode_cli.client.waddleai_auth import _CachedToken

        old_token = _login_response_token(exp_delta=-10)  # already expired
        new_token = _login_response_token(exp_delta=3600)
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return httpx.Response(200, json={"access_token": new_token, "expires_in": 3600})

        config = _make_config(tmp_path)
        provider = WaddleAITokenProvider(config, client_factory=_client_factory_for(handler))
        provider._store.save(
            _CachedToken(
                access_token=old_token,
                expires_at=time.time() - 10,
                issuer=ISSUER,
                audience=AUDIENCE,
                is_dev=False,
            )
        )

        token = await provider.get_access_token()

        assert token == new_token
        assert paths == ["/api/v1/auth/login"], "must not waste a call on a token already expired"


class TestLocalDevFallback:
    @pytest.mark.asyncio
    async def test_no_issuer_configured_yields_local_dev_token(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not make a network call in local-dev fallback")

        provider = WaddleAITokenProvider(
            _make_config(tmp_path, issuer_url=None, username=None, password=None),
            client_factory=_client_factory_for(handler),
            dev_key_path=tmp_path / "dev_key.pem",
        )
        token = await provider.get_access_token()

        claims = jwt.decode(token, options={"verify_signature": False})
        assert claims["tenant"] == "local-dev"
        assert claims["aud"] == AUDIENCE

    @pytest.mark.asyncio
    async def test_explicit_dev_mode_on_local_domain_allowed(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not make a network call in local-dev fallback")

        provider = WaddleAITokenProvider(
            _make_config(
                tmp_path,
                issuer_url="https://waddleai.localhost.local",
                dev_mode=True,
            ),
            client_factory=_client_factory_for(handler),
            dev_key_path=tmp_path / "dev_key.pem",
        )
        token = await provider.get_access_token()
        claims = jwt.decode(token, options={"verify_signature": False})
        assert claims["tenant"] == "local-dev"

    @pytest.mark.asyncio
    async def test_explicit_dev_mode_on_production_looking_issuer_refused(
        self, tmp_path: Path
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not make a network call when the dev gate refuses")

        provider = WaddleAITokenProvider(
            _make_config(tmp_path, issuer_url="https://waddleai.example.com", dev_mode=True),
            client_factory=_client_factory_for(handler),
            dev_key_path=tmp_path / "dev_key.pem",
        )
        with pytest.raises(WaddleAIDevModeRefusedError):
            await provider.get_access_token()

    @pytest.mark.asyncio
    async def test_dev_token_reused_across_calls_via_same_key(self, tmp_path: Path) -> None:
        provider = WaddleAITokenProvider(
            _make_config(tmp_path, issuer_url=None, username=None, password=None),
            dev_key_path=tmp_path / "dev_key.pem",
        )
        first = await provider.get_access_token()
        second = await provider.get_access_token()
        assert first == second, "cached dev token must be reused, not re-minted every call"


class TestClientSideValidation:
    @pytest.mark.asyncio
    async def test_expired_response_token_rejected(self, tmp_path: Path) -> None:
        expired = _login_response_token(exp_delta=-100)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": expired, "expires_in": -100})

        provider = WaddleAITokenProvider(
            _make_config(tmp_path), client_factory=_client_factory_for(handler)
        )
        with pytest.raises(WaddleAITokenInvalidError):
            await provider.get_access_token()

    @pytest.mark.asyncio
    async def test_wrong_audience_response_token_rejected(self, tmp_path: Path) -> None:
        now = int(time.time())
        bad_aud_token = jwt.encode(
            {"sub": "u", "iss": ISSUER, "aud": "someone-else", "iat": now, "exp": now + 3600},
            "test-signing-secret-at-least-32-bytes-long",
            algorithm="HS256",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": bad_aud_token, "expires_in": 3600})

        provider = WaddleAITokenProvider(
            _make_config(tmp_path), client_factory=_client_factory_for(handler)
        )
        with pytest.raises(WaddleAITokenInvalidError):
            await provider.get_access_token()


class TestBearerHeaderAndLogout:
    @pytest.mark.asyncio
    async def test_get_authorization_header_format(self, tmp_path: Path) -> None:
        issued = _login_response_token()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": issued, "expires_in": 3600})

        provider = WaddleAITokenProvider(
            _make_config(tmp_path), client_factory=_client_factory_for(handler)
        )
        header = await provider.get_authorization_header()
        assert header == f"Bearer {issued}"

    @pytest.mark.asyncio
    async def test_logout_clears_cache_and_best_effort_notifies_server(
        self, tmp_path: Path
    ) -> None:
        issued = _login_response_token()
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/api/v1/auth/login":
                return httpx.Response(200, json={"access_token": issued, "expires_in": 3600})
            assert request.url.path == "/api/v1/auth/logout"
            return httpx.Response(200, json={"message": "Logged out successfully"})

        provider = WaddleAITokenProvider(
            _make_config(tmp_path), client_factory=_client_factory_for(handler)
        )
        await provider.get_access_token()
        await provider.logout()

        assert provider._store.load() is None
        assert paths == ["/api/v1/auth/login", "/api/v1/auth/logout"]

    @pytest.mark.asyncio
    async def test_logout_with_no_cached_token_is_a_noop(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not call the server when there is nothing to revoke")

        provider = WaddleAITokenProvider(
            _make_config(tmp_path), client_factory=_client_factory_for(handler)
        )
        await provider.logout()
        assert provider._store.load() is None


class TestNeverLogsToken:
    @pytest.mark.asyncio
    async def test_token_value_never_appears_in_log_records(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        issued = _login_response_token()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": issued, "expires_in": 3600})

        caplog.set_level("DEBUG")
        provider = WaddleAITokenProvider(
            _make_config(tmp_path), client_factory=_client_factory_for(handler)
        )
        await provider.get_access_token()

        for record in caplog.records:
            assert issued not in record.getMessage()
