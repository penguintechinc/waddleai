"""Test doubles for classification auth/server tests: RSA keys, tokens, engine, servicer."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from services.classification.app.auth import REQUIRED_SCOPE, build_verifier
from services.classification.app.engine import RawEntity
from services.classification.app.failmode import PolicyOutcome
from services.classification.app.grpc_gen import classification_pb2 as pb

ISSUER = "https://auth.example.test"
AUDIENCE = "classification-test"
KID = "test-kid-1"
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@dataclass(slots=True)
class _Key:
    """Object exposing ``.key`` like PyJWK."""

    key: Any


class StaticResolver:
    """Resolver returning the test public key; can simulate a JWKS outage."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        """Start healthy unless ``fail_with`` is given."""
        self.fail_with = fail_with
        self.calls = 0

    def get_signing_key_from_jwt(self, token: str) -> _Key:
        """Return the key or raise the configured failure."""
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return _Key(PRIVATE_KEY.public_key())


def make_token(
    *,
    scope: list[str] | None = None,
    exp_delta: timedelta = timedelta(hours=1),
    key: Any = PRIVATE_KEY,
    audience: str = AUDIENCE,
    issuer: str = ISSUER,
    kid: str = KID,
) -> str:
    """Sign an RS256 token for the test issuer (obviously fake claims)."""
    now = datetime.now(UTC)
    claims = {
        "sub": "svc-proxy",
        "iss": issuer,
        "aud": audience,
        "iat": now - timedelta(minutes=5),
        "exp": now + exp_delta,
        "tenant": "tenant-test",
        "scope": scope if scope is not None else [REQUIRED_SCOPE],
        "teams": [],
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def make_verifier(resolver: StaticResolver | None = None) -> Any:
    """Build the real penguin-aaa-backed verifier over the static resolver."""
    return build_verifier(
        ISSUER, AUDIENCE, f"{ISSUER}/.well-known/jwks.json", resolver or StaticResolver()
    )


class FakeEngine:
    """Engine double: ready flag, scripted entities, optional slow analyze."""

    backend = "presidio"
    fingerprint = "presidio|m|1|presidio=2"

    def __init__(self, *, ready: bool = True, delay: float = 0.0) -> None:
        """Configure readiness and per-call delay."""
        self.ready = ready
        self.delay = delay
        self.closed = False
        self.started = False
        self.started_event = asyncio.Event()

    async def start(self) -> None:
        """Record the start (readiness is driven by the test)."""
        self.started = True

    async def aclose(self) -> None:
        """Record close and drop readiness."""
        self.closed = True
        self.ready = False

    async def analyze(self, text: str, language: str, deadline_s: float) -> list[RawEntity]:
        """Return one PERSON span after the configured delay."""
        self.started_event.set()
        if self.delay:
            await asyncio.sleep(self.delay)
        return [RawEntity("PERSON", 0, 5, 0.9)]


class FakeCache:
    """Cache double (miss always, no-op put)."""

    def key(self, fingerprint: str, org_public_id: str, text: str) -> str:
        """Deterministic key."""
        return f"{fingerprint}:{org_public_id}:{text}"

    async def get(self, key: str) -> list[RawEntity] | None:
        """Always miss."""
        return None

    async def put(self, key: str, raw: list[RawEntity]) -> None:
        """Discard."""


@dataclass(slots=True)
class FakeGate:
    """Gate double that is always on."""

    seen: list[str] = field(default_factory=list)

    async def enabled(self, flag_distinct_id: str) -> bool:
        """Allow."""
        return True


class FakePolicy:
    """Policy double (fail-open)."""

    async def apply(self, **kw: Any) -> PolicyOutcome:
        """Return a degraded fail-open outcome."""
        return PolicyOutcome(
            degraded=True, block_recommended=False, effective_mode=pb.FAIL_MODE_OPEN
        )
