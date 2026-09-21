"""Authentication service implementation with JWT."""

import hmac
import logging
import secrets
import time

import grpc
import jwt

from penguincode_cli.config.settings import AuthConfig
from penguincode_cli.proto import (
    AuthRequest,
    AuthResponse,
    AuthServiceServicer,
    RefreshRequest,
    ValidateRequest,
    ValidateResponse,
)

logger = logging.getLogger(__name__)


class AuthServiceImpl(AuthServiceServicer):
    """JWT-based authentication service.

    Handles API key validation, JWT token generation, and token refresh.
    """

    def __init__(self, config: AuthConfig):
        self.config = config
        self.jwt_secret = config.jwt_secret or secrets.token_hex(32)
        self.token_expiry = config.token_expiry
        self.refresh_expiry = config.refresh_expiry
        self.valid_api_keys = set(config.api_keys)
        self.shared_key = config.shared_key

        # Store refresh tokens (in production, use Redis or database)
        self._refresh_tokens: dict[str, tuple[str, int]] = {}  # token -> (user_id, expires_at)
        # Tokens already rotated out, retained so a replay is detectable rather than
        # indistinguishable from an ordinary bad token.
        self._rotated_refresh_tokens: dict[str, str] = {}  # rotated token -> user_id

    async def Authenticate(
        self,
        request: AuthRequest,
        context: grpc.aio.ServicerContext,
    ) -> AuthResponse:
        """Authenticate with API key or shared key and return JWT tokens."""
        # Check shared key first, then API keys — constant-time throughout
        key_valid = self._credential_matches(request.api_key)
        if not key_valid:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "Invalid API key",
            )

        # Generate tokens
        user_id = request.client_id or f"client_{secrets.token_hex(8)}"
        access_token = self._generate_access_token(user_id)
        refresh_token = self._generate_refresh_token(user_id)

        return AuthResponse(
            access_token=access_token,
            expires_in=self.token_expiry,
            refresh_token=refresh_token,
        )

    async def RefreshToken(
        self,
        request: RefreshRequest,
        context: grpc.aio.ServicerContext,
    ) -> AuthResponse:
        """Refresh an access token, enforcing expiry and detecting token reuse.

        Refresh tokens rotate on every use. Presenting a token that was already
        rotated out means the chain leaked, so the whole chain is revoked rather
        than the replay simply being rejected.
        """
        presented = request.refresh_token

        # Reuse detection before anything else: a rotated-out token is a replay.
        replayed_user = self._rotated_refresh_tokens.get(presented)
        if replayed_user is not None:
            revoked = self._revoke_user_refresh_tokens(replayed_user)
            logger.error(
                "Refresh token reuse detected for user %s - revoked %d active refresh token(s)",
                replayed_user,
                revoked,
            )
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "Refresh token reuse detected - session revoked",
            )

        entry = self._refresh_tokens.get(presented)
        if entry is None:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "Invalid refresh token",
            )

        user_id, expires_at = entry
        if int(time.time()) >= expires_at:
            del self._refresh_tokens[presented]
            logger.info("Rejected expired refresh token for user %s", user_id)
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "Refresh token expired",
            )

        # Generate new tokens
        access_token = self._generate_access_token(user_id)
        new_refresh_token = self._generate_refresh_token(user_id)

        # Rotate: retire the presented token and remember it for reuse detection
        del self._refresh_tokens[presented]
        self._rotated_refresh_tokens[presented] = user_id

        return AuthResponse(
            access_token=access_token,
            expires_in=self.token_expiry,
            refresh_token=new_refresh_token,
        )

    async def ValidateToken(
        self,
        request: ValidateRequest,
        context: grpc.aio.ServicerContext,
    ) -> ValidateResponse:
        """Validate an access token."""
        claims = self._validate_access_token(request.access_token)

        if claims is None:
            return ValidateResponse(
                valid=False,
                user_id="",
                scopes=[],
            )

        return ValidateResponse(
            valid=True,
            user_id=claims.get("sub", ""),
            scopes=claims.get("scopes", ["chat", "tools"]),
        )

    def _generate_access_token(self, user_id: str) -> str:
        """Generate a JWT access token."""
        now = int(time.time())
        payload = {
            "sub": user_id,
            "iat": now,
            "exp": now + self.token_expiry,
            "scopes": ["chat", "tools"],
            "type": "access",
        }
        return jwt.encode(payload, self.jwt_secret, algorithm="HS256")

    def _credential_matches(self, presented: str) -> bool:
        """Compare a presented credential against the shared key and API keys.

        Uses hmac.compare_digest and checks every configured key without
        short-circuiting, so neither the key contents nor which key matched leaks
        through response timing.
        """
        presented_bytes = presented.encode("utf-8")
        matched = False
        if self.shared_key:
            matched = hmac.compare_digest(presented_bytes, self.shared_key.encode("utf-8")) or matched
        for api_key in self.valid_api_keys:
            matched = hmac.compare_digest(presented_bytes, api_key.encode("utf-8")) or matched
        return matched

    def _revoke_user_refresh_tokens(self, user_id: str) -> int:
        """Revoke every active refresh token held for a user, returning the count.

        Revoked tokens move into the rotated set so a later replay of any of them
        is still recognised as reuse rather than an ordinary unknown token.
        """
        doomed = [token for token, (owner, _) in self._refresh_tokens.items() if owner == user_id]
        for token in doomed:
            del self._refresh_tokens[token]
            self._rotated_refresh_tokens[token] = user_id
        return len(doomed)

    def _generate_refresh_token(self, user_id: str) -> str:
        """Generate a refresh token stamped with the configured refresh expiry."""
        token = secrets.token_urlsafe(32)
        self._refresh_tokens[token] = (user_id, int(time.time()) + self.refresh_expiry)
        return token

    def _validate_access_token(self, token: str) -> dict | None:
        """Validate and decode an access token.

        Returns claims dict if valid, None otherwise.
        """
        try:
            claims = jwt.decode(
                token,
                self.jwt_secret,
                algorithms=["HS256"],
            )
            if claims.get("type") != "access":
                return None
            return claims
        except jwt.ExpiredSignatureError:
            return None
        except jwt.InvalidTokenError:
            return None
