"""Authentication service implementation with JWT."""

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
        self._refresh_tokens: dict[str, str] = {}  # refresh_token -> user_id

    async def Authenticate(
        self,
        request: AuthRequest,
        context: grpc.aio.ServicerContext,
    ) -> AuthResponse:
        """Authenticate with API key or shared key and return JWT tokens."""
        # Check shared key first, then API keys
        key_valid = (self.shared_key and request.api_key == self.shared_key) or request.api_key in self.valid_api_keys
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
        """Refresh an access token using a refresh token."""
        # Validate refresh token
        user_id = self._refresh_tokens.get(request.refresh_token)
        if not user_id:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "Invalid refresh token",
            )

        # Generate new tokens
        access_token = self._generate_access_token(user_id)
        new_refresh_token = self._generate_refresh_token(user_id)

        # Invalidate old refresh token
        del self._refresh_tokens[request.refresh_token]

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

    def _generate_refresh_token(self, user_id: str) -> str:
        """Generate a refresh token."""
        token = secrets.token_urlsafe(32)
        self._refresh_tokens[token] = user_id
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
