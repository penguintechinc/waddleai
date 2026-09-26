"""CLI acquisition of a WaddleAI-issued RS256 JWT (F4).

Feasibility finding (Step 0 of the F4 task): WaddleAI's management service
exposes exactly one CLI-usable path to an RS256 JWT today --
``POST /api/v1/auth/login`` (``services/management/app/api/v1/auth.py``),
which requires an interactive human credential (username + password) and
returns ``{access_token, token_type, expires_in}`` -- there is **no**
``refresh_token`` in that response. Renewal instead goes through
``POST /api/v1/auth/refresh``, which rotates a still-*unexpired* bearer token
for a fresh one (revokes the old ``jti`` and mints a new token) rather than
redeeming a separate refresh grant. There is no OIDC discovery document, no
published JWKS endpoint (``shared.auth.penguin_auth.LocalOIDCRelyingParty``'s
docstring confirms this explicitly: "there is no external OIDC issuer and no
published JWKS endpoint"), no device-code flow, and no API-key-to-JWT
exchange endpoint -- ``keys.py``'s API keys are validated as a bearer
credential in their own right (``verify_api_key`` inside
``auth.py``'s ``require_auth``), never exchanged for a signed JWT.

**Server-side gap this exposes** (out of scope for F4, flagged for whoever
picks up a machine-auth follow-up): a CLI running non-interactively (CI,
headless agents) cannot obtain a WaddleAI JWT today without embedding a
username/password in the environment, because no client-credentials or
device-code grant exists. This module is written against the *intended*
interface (``WaddleAIAuthConfig`` deliberately shaped so wiring a future
``grant_type=client_credentials`` call in requires no consumer-facing
change) plus today's real login/refresh endpoints, plus a local-dev fallback
for the common single-tenant case where no WaddleAI deployment exists yet.

Claims shape produced by ``/auth/login``/``/auth/refresh`` (see
``shared.auth.penguin_auth.user_context_to_claims``) matches what
``penguincode_cli.auth.middleware.WaddleAIJWTValidator``/``scope_from_claims``
expect (``sub``, ``iss``, ``aud``, ``tenant``, ``teams``, ``scope``) with one
caveat worth flagging: WaddleAI tokens carry no separate ``org`` claim today
(``tenant`` is the user's ``organization_id``; ``teams`` is their
``managed_orgs``) -- this module does not paper over that, it just passes
whatever claims the server issues through unmodified.

**H5 addendum** -- the server-side gap flagged above is closed: WaddleAI now
exposes ``POST /api/v1/auth/token`` (H2), a client-credentials-shaped
exchange for a service-account ``wa-`` API key -- no body, credential only
via ``Authorization: Bearer wa-<key>``, returning the same
``{access_token, token_type, expires_in}`` shape as login/refresh. This adds
a **headless/machine mode** to :class:`WaddleAITokenProvider`: when
``WADDLEAI_API_KEY``/``WADDLEAI_API_KEY_FILE`` names a service-account key,
it takes precedence over interactive login and the local-dev fallback alike
(no prompts, no TTY required -- CI-safe). There is no ``refresh_token`` in
this flow either, so renewal re-exchanges the key rather than calling
``/auth/refresh`` (a machine-issued token cannot be rotated that way -- only
a real login-issued token can). The key itself is never cached, never
logged, and never appears in an error message unmasked.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

logger = logging.getLogger(__name__)

_DEFAULT_ISSUER = "https://waddleai.localhost.local"
_DEFAULT_AUDIENCE = "waddleai-api"
_LOGIN_PATH = "/api/v1/auth/login"
_REFRESH_PATH = "/api/v1/auth/refresh"
_LOGOUT_PATH = "/api/v1/auth/logout"
_TOKEN_EXCHANGE_PATH = "/api/v1/auth/token"
_API_KEY_ENV = "WADDLEAI_API_KEY"  # nosec B105 -- an env var name, not a credential
_API_KEY_FILE_ENV = "WADDLEAI_API_KEY_FILE"  # nosec B105 -- an env var name, not a credential
_DEFAULT_TOKEN_PATH = "~/.penguincode/waddleai_token.json"  # nosec B105 -- a file path, not a credential
_DEFAULT_DEV_KEY_PATH = "~/.penguincode/waddleai_dev_key.pem"

#: Domains a local-dev token is permitted to be minted against -- mirrors the
#: license-bypass domain list (penguintech.md) plus the local-dev TLD
#: (`{repo}.localhost.local`). Anything else is treated as "looks like a real
#: WaddleAI deployment" and refuses the fallback (fail closed).
_DEV_SAFE_SUFFIXES = (".localhost.local", ".penguintech.cloud", ".penguincloud.io")
_DEV_SAFE_HOSTS = ("localhost", "127.0.0.1")

_TRUTHY = ("1", "true", "yes", "on")


def _mask_key(key: str) -> str:
    """Mask a service-account key for logs/error messages -- e.g. ``wa-****1234``.

    Never returns enough of the key to be reused; used everywhere a machine
    key might otherwise leak into a log record or an exception message.
    """
    tail = key[-4:] if len(key) >= 4 else "*" * len(key)
    return f"wa-****{tail}"


class WaddleAIAuthError(Exception):
    """Base error for all WaddleAI token-acquisition failures.

    The single exception type callers (F3's gRPC client) need to catch --
    every failure mode below subclasses this.
    """


class WaddleAICredentialsError(WaddleAIAuthError):
    """No usable credential was available (no cache, no env, no dev fallback)."""


class WaddleAIDevModeRefusedError(WaddleAIAuthError):
    """The local-dev fallback was needed/requested but the fail-closed gate refused it.

    Raised rather than silently falling through, so a misconfigured
    ``--dev``/``WADDLEAI_DEV_MODE`` flag against a real deployment never
    produces a token that a real server will simply reject downstream --
    the CLI fails loudly and immediately instead.
    """


class WaddleAITokenInvalidError(WaddleAIAuthError):
    """A token (freshly issued or cached) failed client-side exp/aud/iss validation."""


@dataclass(slots=True, frozen=True)
class WaddleAIAuthConfig:
    """Env-sourced configuration for acquiring a WaddleAI RS256 JWT.

    Mirrors the env-var naming of the server-side
    ``penguincode_cli.auth.middleware.JWTValidatorConfig`` (``issuer``/
    ``audience``) so a client and the server it talks to agree on defaults,
    without importing that module -- F4 and F2 stay independent.
    """

    issuer_url: str | None
    username: str | None
    password: str | None
    machine_key: str | None
    audience: str
    token_path: str
    dev_mode: bool
    request_timeout: float
    refresh_leeway_seconds: int

    @classmethod
    def from_env(cls, *, dev_mode: bool = False) -> WaddleAIAuthConfig:
        """Build config from ``WADDLEAI_*`` env vars (never CLI args -- see security.md).

        *dev_mode* is an explicit caller override (e.g. a CLI ``--dev`` flag)
        that ORs with ``WADDLEAI_DEV_MODE`` -- either is sufficient to opt in.
        """
        issuer = os.environ.get("WADDLEAI_ISSUER_URL", "").strip() or None
        env_dev = os.environ.get("WADDLEAI_DEV_MODE", "").strip().lower() in _TRUTHY
        return cls(
            issuer_url=issuer.rstrip("/") if issuer else None,
            username=os.environ.get("WADDLEAI_USERNAME") or None,
            password=os.environ.get("WADDLEAI_PASSWORD") or None,
            machine_key=cls._load_machine_key(),
            audience=os.environ.get("WADDLEAI_JWT_AUDIENCE", _DEFAULT_AUDIENCE),
            token_path=os.environ.get("WADDLEAI_TOKEN_PATH", _DEFAULT_TOKEN_PATH),
            dev_mode=dev_mode or env_dev,
            request_timeout=float(os.environ.get("WADDLEAI_AUTH_TIMEOUT_SECONDS", "10")),
            refresh_leeway_seconds=int(os.environ.get("WADDLEAI_REFRESH_LEEWAY_SECONDS", "60")),
        )

    @staticmethod
    def _load_machine_key() -> str | None:
        """Load a service-account key from env or a mounted-secret file -- never a CLI arg.

        ``WADDLEAI_API_KEY`` wins if both are set; ``WADDLEAI_API_KEY_FILE``
        covers the mounted-secret case (K8s Secret volume, CI secret file).
        A configured-but-unreadable/empty file is a hard misconfiguration --
        this fails fast rather than silently falling through to an
        interactive login prompt that would simply hang with no TTY (CI).
        """
        env_key = os.environ.get(_API_KEY_ENV, "").strip()
        if env_key:
            return env_key

        key_file = os.environ.get(_API_KEY_FILE_ENV, "").strip()
        if not key_file:
            return None

        try:
            contents = Path(key_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise WaddleAICredentialsError(
                f"{_API_KEY_FILE_ENV}={key_file!r} could not be read: {exc}"
            ) from exc
        if not contents:
            raise WaddleAICredentialsError(f"{_API_KEY_FILE_ENV}={key_file!r} is empty")
        return contents


@dataclass(slots=True)
class _CachedToken:
    """On-disk representation of the last token this provider acquired."""

    access_token: str
    expires_at: float
    issuer: str
    audience: str
    is_dev: bool
    is_machine: bool = False

    def to_json(self) -> dict[str, Any]:
        """Serialize for ``WaddleAITokenStore.save`` -- never includes anything beyond these fields."""
        return {
            "access_token": self.access_token,
            "expires_at": self.expires_at,
            "issuer": self.issuer,
            "audience": self.audience,
            "is_dev": self.is_dev,
            "is_machine": self.is_machine,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> _CachedToken:
        """Rebuild from a parsed cache file; raises on a shape the cache never wrote (see ``load``).

        ``is_machine`` defaults to ``False`` for a cache file written before
        H5 -- an older cache never set the key, and it must be treated as a
        non-machine (login/dev) token, never crash on the missing field.
        """
        return cls(
            access_token=str(data["access_token"]),
            expires_at=float(data["expires_at"]),
            issuer=str(data.get("issuer") or ""),
            audience=str(data.get("audience") or ""),
            is_dev=bool(data.get("is_dev", False)),
            is_machine=bool(data.get("is_machine", False)),
        )


class WaddleAITokenStore:
    """Owner-only-permission JSON cache for a single WaddleAI token.

    Deliberately a separate file from ``client.auth.TokenManager``'s
    ``~/.penguincode/token`` -- that file holds the *local* client-server
    HS256 credential used by the existing ``AuthService/Authenticate`` RPC,
    an entirely different token for an entirely different purpose (see
    ``penguincode_cli/auth/middleware.py``'s module docstring). Sharing one
    file would let a WaddleAI refresh silently clobber the local session, or
    vice versa.
    """

    def __init__(self, path: str) -> None:
        """Bind this store to *path* (``~`` expanded once, at construction)."""
        self._path = Path(path).expanduser()

    def load(self) -> _CachedToken | None:
        """Return the cached token, or ``None`` if absent/corrupt (never raises)."""
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return _CachedToken.from_json(data)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.debug("waddleai_auth: discarding unreadable token cache: %s", exc)
            return None

    def save(self, token: _CachedToken) -> None:
        """Persist *token*, creating the parent directory and restricting permissions to owner-only."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(token.to_json()), encoding="utf-8")
        os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)

    def clear(self) -> None:
        """Delete the cache file, if present."""
        if self._path.exists():
            self._path.unlink()


class WaddleAITokenProvider:
    """Acquires, caches, and refreshes a tenant-scoped WaddleAI RS256 JWT for the CLI.

    Consumed by F3's gRPC client via :meth:`get_access_token` or
    :meth:`get_authorization_header` to populate the ``authorization``
    invocation-metadata entry that ``penguincode_cli.auth.middleware``
    validates server-side. Mode precedence in :meth:`get_access_token`:

    1. A cached token, reused as-is until within ``refresh_leeway_seconds``
       of its own ``expires_at`` (checked regardless of mode).
    2. ``POST {issuer}/api/v1/auth/refresh`` -- only for a still-unexpired,
       *non-machine*, non-dev cached token (WaddleAI's refresh endpoint
       rejects an already-expired bearer token outright, so this is skipped
       once truly expired rather than wasting a guaranteed-401 round trip;
       a machine-issued token is never refreshed this way -- see step 3).
    3. **Headless/machine mode** -- :meth:`_exchange_machine_key`, taken
       whenever ``config.machine_key`` is set (``WADDLEAI_API_KEY``/
       ``WADDLEAI_API_KEY_FILE``). Wins over interactive login and the
       local-dev fallback alike, no prompts, no TTY required. Renewal
       re-exchanges the key (step 2 never applies to a machine token).
    4. ``POST {issuer}/api/v1/auth/login`` with ``username``/``password`` --
       the interactive initial-acquisition path (see module docstring).
    5. :meth:`_issue_dev_token` -- a locally self-signed fallback, used only
       when no real issuer is configured or ``dev_mode`` is explicitly set
       (and, in that explicit case, only against a domain the fail-closed
       gate recognises as local/PenguinTech-controlled).
    """

    def __init__(
        self,
        config: WaddleAIAuthConfig | None = None,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        store: WaddleAITokenStore | None = None,
        clock: Callable[[], float] = time.time,
        dev_key_path: str | Path | None = None,
    ) -> None:
        """Bind this provider to *config* (defaults to ``WaddleAIAuthConfig.from_env()``).

        *client_factory*/*store*/*clock*/*dev_key_path* are test seams --
        production code leaves all four at their defaults.
        """
        self._config = config or WaddleAIAuthConfig.from_env()
        self._client_factory = client_factory or self._default_client_factory
        self._store = store or WaddleAITokenStore(self._config.token_path)
        self._clock = clock
        self._dev_key_path = (
            Path(dev_key_path) if dev_key_path else Path(_DEFAULT_DEV_KEY_PATH).expanduser()
        )

    def _default_client_factory(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._config.request_timeout)

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        """Return a valid bearer token, acquiring/refreshing/caching as needed.

        Raises a :class:`WaddleAIAuthError` subclass on any failure -- callers
        (F3) should treat any of them as "could not authenticate this call".
        """
        cached = None if force_refresh else self._store.load()
        now = self._clock()

        if cached is not None and self._cache_matches_current_config(cached):
            if now < cached.expires_at - self._config.refresh_leeway_seconds:
                self._validate_claims(cached.access_token)
                return cached.access_token
            if now < cached.expires_at and not cached.is_dev and not cached.is_machine:
                try:
                    return await self._refresh(cached.access_token)
                except WaddleAIAuthError as exc:
                    logger.info("waddleai_auth: refresh failed, re-acquiring: %s", exc)

        if self._config.machine_key:
            return await self._exchange_machine_key()

        if self._should_use_dev_fallback():
            return self._issue_dev_token()

        return await self._login()

    async def get_authorization_header(self) -> str:
        """Return ``"Bearer <token>"``, ready to drop straight into a header/metadata value."""
        token = await self.get_access_token()
        return f"Bearer {token}"

    async def get_auth_metadata(self) -> list[tuple[str, str]]:
        """Return gRPC invocation metadata carrying the bearer token (F3's consumption point)."""
        return [("authorization", await self.get_authorization_header())]

    async def logout(self) -> None:
        """Discard the cached token and best-effort notify the server (``/auth/logout``).

        A dev-issued token has no server-side session to revoke, so the
        network call is skipped for those. Server-side failure is logged,
        never raised -- the local discard is what matters to the caller.
        """
        cached = self._store.load()
        self._store.clear()
        if cached is None or cached.is_dev:
            return

        issuer = self._effective_issuer()
        try:
            async with self._client_factory() as client:
                await client.post(
                    f"{issuer}{_LOGOUT_PATH}",
                    headers={"Authorization": f"Bearer {cached.access_token}"},
                )
        except httpx.HTTPError as exc:
            logger.debug("waddleai_auth: best-effort server-side logout failed: %s", exc)

    def _cache_matches_current_config(self, cached: _CachedToken) -> bool:
        """A cached token is only reusable if it was issued for the same issuer/audience/mode."""
        return (
            cached.issuer == (self._config.issuer_url or "")
            and cached.audience == self._config.audience
            and cached.is_dev == self._effective_dev_mode()
            and cached.is_machine == bool(self._config.machine_key)
        )

    def _effective_dev_mode(self) -> bool:
        """Whether the *next* acquisition would use the dev fallback.

        True both when the caller explicitly opted in (``dev_mode=True``) and
        when no issuer is configured at all -- the latter implies dev mode
        even though ``config.dev_mode`` itself is left at its default
        ``False``, and cache-reuse must agree with that or every call would
        re-mint a fresh dev token. A configured machine key always wins --
        headless mode is checked first in :meth:`get_access_token`, so dev
        mode is never actually entered while one is set, but this keeps
        :meth:`_cache_matches_current_config` consistent either way.
        """
        if self._config.machine_key:
            return False
        return self._config.dev_mode or not self._config.issuer_url

    def _effective_issuer(self) -> str:
        return self._config.issuer_url or _DEFAULT_ISSUER

    async def _login(self) -> str:
        """Acquire a fresh token via ``POST /api/v1/auth/login``."""
        if not self._config.username:
            raise WaddleAICredentialsError(
                "no WaddleAI credentials configured -- set WADDLEAI_USERNAME/WADDLEAI_PASSWORD, "
                "or leave WADDLEAI_ISSUER_URL unset / pass dev_mode=True for local development"
            )
        password = self._config.password or self._prompt_password(self._config.username)
        if not password:
            raise WaddleAICredentialsError(
                "no WADDLEAI_PASSWORD set and no interactive password was available"
            )

        issuer = self._effective_issuer()
        try:
            async with self._client_factory() as client:
                response = await client.post(
                    f"{issuer}{_LOGIN_PATH}",
                    json={"username": self._config.username, "password": password},
                )
        except httpx.HTTPError as exc:
            raise WaddleAIAuthError(f"WaddleAI login request failed: {exc}") from exc

        if response.status_code != 200:
            raise WaddleAIAuthError(f"WaddleAI login rejected (HTTP {response.status_code})")

        token, expires_in = self._parse_token_response(response, context="login")
        self._validate_claims(token)
        self._cache(token, expires_in, is_dev=False)
        logger.info("waddleai_auth: acquired token via login (expires_in=%ss)", expires_in)
        return token

    async def _refresh(self, old_token: str) -> str:
        """Rotate a still-valid *old_token* via ``POST /api/v1/auth/refresh``."""
        issuer = self._effective_issuer()
        try:
            async with self._client_factory() as client:
                response = await client.post(
                    f"{issuer}{_REFRESH_PATH}",
                    headers={"Authorization": f"Bearer {old_token}"},
                )
        except httpx.HTTPError as exc:
            raise WaddleAIAuthError(f"WaddleAI refresh request failed: {exc}") from exc

        if response.status_code != 200:
            raise WaddleAIAuthError(f"WaddleAI refresh rejected (HTTP {response.status_code})")

        token, expires_in = self._parse_token_response(response, context="refresh")
        self._validate_claims(token)
        self._cache(token, expires_in, is_dev=False)
        logger.info("waddleai_auth: refreshed token (expires_in=%ss)", expires_in)
        return token

    async def _exchange_machine_key(self) -> str:
        """Exchange the configured service-account key for a fresh JWT (H2's ``/auth/token``).

        No body, no username/password -- the key travels only as a bearer
        credential (``Authorization: Bearer wa-<key>``), matching H2's
        contract. There is no refresh grant for a machine-issued token, so
        this same method is also how :meth:`get_access_token` renews one
        near/at expiry -- a re-exchange, never ``/auth/refresh``. A ``401``
        means the key is invalid or disabled: the cache is cleared so a
        stale/bad key is never silently retried, and a clear
        :class:`WaddleAIAuthError` is raised (never a raw traceback, never
        the unmasked key).
        """
        key = self._config.machine_key
        if not key:
            raise WaddleAICredentialsError(
                "no WaddleAI machine key configured -- set WADDLEAI_API_KEY or "
                "WADDLEAI_API_KEY_FILE for headless/CI authentication"
            )

        issuer = self._effective_issuer()
        try:
            async with self._client_factory() as client:
                response = await client.post(
                    f"{issuer}{_TOKEN_EXCHANGE_PATH}",
                    headers={"Authorization": f"Bearer {key}"},
                )
        except httpx.HTTPError as exc:
            raise WaddleAIAuthError(f"WaddleAI machine-key exchange request failed: {exc}") from exc

        if response.status_code == 401:
            self._store.clear()
            raise WaddleAIAuthError(
                f"WaddleAI machine key rejected (invalid or disabled): {_mask_key(key)}"
            )
        if response.status_code != 200:
            raise WaddleAIAuthError(
                f"WaddleAI machine-key exchange rejected (HTTP {response.status_code})"
            )

        token, expires_in = self._parse_token_response(response, context="machine-key exchange")
        self._validate_claims(token)
        self._cache(token, expires_in, is_dev=False, is_machine=True)
        logger.info(
            "waddleai_auth: acquired token via machine-key exchange (key=%s, expires_in=%ss)",
            _mask_key(key),
            expires_in,
        )
        return token

    @staticmethod
    def _parse_token_response(response: httpx.Response, *, context: str) -> tuple[str, float]:
        """Extract ``(access_token, expires_in)`` from a login/refresh response body."""
        body: Any = response.json()
        token = body.get("access_token") if isinstance(body, dict) else None
        expires_in = body.get("expires_in") if isinstance(body, dict) else None
        if not token or not isinstance(expires_in, int | float):
            raise WaddleAIAuthError(f"WaddleAI {context} response missing access_token/expires_in")
        return str(token), float(expires_in)

    def _cache(
        self, token: str, expires_in: float, *, is_dev: bool, is_machine: bool = False
    ) -> None:
        self._store.save(
            _CachedToken(
                access_token=token,
                expires_at=self._clock() + expires_in,
                issuer=self._config.issuer_url or "",
                audience=self._config.audience,
                is_dev=is_dev,
                is_machine=is_machine,
            )
        )

    def _validate_claims(self, token: str) -> dict[str, Any]:
        """Fail-fast client-side check: signature is NOT verified here.

        The CLI trusts TLS plus the server that just handed it the token; the
        point of this check is to catch clock skew, misconfiguration, or a
        server bug (wrong audience/issuer) immediately rather than have it
        surface as an opaque UNAUTHENTICATED from the gRPC server later.
        Real signature verification happens server-side
        (``penguincode_cli.auth.middleware.WaddleAIJWTValidator``).
        """
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                options={"verify_signature": False, "verify_exp": True, "verify_aud": True},
                audience=self._config.audience,
            )
        except jwt.ExpiredSignatureError as exc:
            raise WaddleAITokenInvalidError("token is expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise WaddleAITokenInvalidError(
                f"token audience mismatch (expected {self._config.audience!r})"
            ) from exc
        except jwt.PyJWTError as exc:
            raise WaddleAITokenInvalidError(f"token failed client-side validation: {exc}") from exc

        expected_issuer = self._effective_issuer()
        if claims.get("iss") != expected_issuer:
            raise WaddleAITokenInvalidError(
                f"token issuer {claims.get('iss')!r} does not match configured issuer "
                f"{expected_issuer!r}"
            )
        return claims

    @staticmethod
    def _prompt_password(username: str) -> str | None:
        """Interactively prompt for a password when stdin is a real terminal.

        Never a CLI argument (security.md Token & Secret Hygiene) and never
        echoed (``getpass``); returns ``None`` in any non-interactive context
        so a headless caller gets a clear :class:`WaddleAICredentialsError`
        instead of hanging on stdin.
        """
        if not sys.stdin.isatty():
            return None
        return getpass.getpass(f"WaddleAI password for {username}: ")

    def _should_use_dev_fallback(self) -> bool:
        """Decide whether :meth:`_issue_dev_token` should run.

        Two ways in: no issuer configured at all (the common "no WaddleAI
        deployment yet" case), or an explicit ``dev_mode`` opt-in -- which is
        itself gated fail-closed against the configured issuer's domain, so
        an explicit ``--dev`` against a real deployment raises rather than
        silently downgrading security.
        """
        if not self._config.issuer_url:
            return True
        if not self._config.dev_mode:
            return False
        if self._issuer_is_dev_safe(self._config.issuer_url):
            return True
        raise WaddleAIDevModeRefusedError(
            f"--dev refused: {self._config.issuer_url!r} is not a recognised local/"
            "PenguinTech-controlled domain -- local-dev tokens are not valid against a "
            "real WaddleAI deployment and will not be minted against it"
        )

    @staticmethod
    def _issuer_is_dev_safe(issuer_url: str) -> bool:
        host = httpx.URL(issuer_url).host or ""
        return host in _DEV_SAFE_HOSTS or any(
            host.endswith(suffix) for suffix in _DEV_SAFE_SUFFIXES
        )

    def _issue_dev_token(self) -> str:
        """Mint a locally self-signed RS256 token for single-tenant local development.

        Fail-closed gating happened already in :meth:`_should_use_dev_fallback`
        -- by the time this runs, either no issuer was configured at all, or
        an explicit ``--dev`` was confirmed safe. The banner below is
        mandatory (org ``--dev`` convention): a dev token must never look
        like a normal, silent success.
        """
        sys.stderr.write(
            "\n"
            "*** WaddleAI local-dev mode: using a locally self-signed token. ***\n"
            "*** Single-tenant only -- this token is NOT valid against a real WaddleAI server. ***\n"
            "*** Set WADDLEAI_ISSUER_URL + WADDLEAI_USERNAME/WADDLEAI_PASSWORD for real auth. ***\n\n"
        )
        private_key_pem = self._load_or_create_dev_key()
        issuer = self._effective_issuer()
        now = int(self._clock())
        expires_in = 3600
        claims = {
            "sub": "local-dev-user",
            "iss": issuer,
            "aud": self._config.audience,
            "iat": now,
            "exp": now + expires_in,
            "tenant": "local-dev",
            "teams": [],
            "scope": ["*"],
        }
        token = jwt.encode(claims, private_key_pem, algorithm="RS256")
        self._validate_claims(token)
        self._cache(token, float(expires_in), is_dev=True)
        logger.warning("waddleai_auth: issued a local-dev token (tenant=local-dev)")
        return token

    def _load_or_create_dev_key(self) -> str:
        """Load the persisted dev RSA key, generating and caching one on first use."""
        if self._dev_key_path.exists():
            return self._dev_key_path.read_text(encoding="utf-8")

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
        self._dev_key_path.parent.mkdir(parents=True, exist_ok=True)
        self._dev_key_path.write_text(pem, encoding="utf-8")
        os.chmod(self._dev_key_path, stat.S_IRUSR | stat.S_IWUSR)
        return pem
