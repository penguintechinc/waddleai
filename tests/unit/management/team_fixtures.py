"""Teams-subsystem fixtures for the management tests (moved out of ``conftest.py``).

``conftest.py`` imports these names so pytest registers them; they live here so that file
stays smaller. They use only fixtures defined elsewhere by name (``flask_app``,
``team_world``, ``mint_token``).
"""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest

#: Route modules whose module-level ``db`` the real-database fixture below repoints.
_REAL_DB_ROUTE_MODULES = ("auth", "users", "organizations", "keys", "proxy_keys", "teams")


@pytest.fixture
async def real_db_client(flask_app, team_world, monkeypatch) -> AsyncGenerator:
    """A Quart test client whose route modules talk to ``team_world``'s REAL database.

    ``flask_app`` patches every route module's ``db`` with one shared MagicMock; this repoints
    the modules the teams tests exercise at the real SQLAlchemy-schema database instead, and
    resets the process-wide auth singletons (throttle, rate limiter, denylist) so a lockout in
    one test cannot leak into the next.
    """
    import importlib

    from services.management.app.services.login_throttle import reset_login_throttle
    from services.management.app.services.rate_limiter import reset_auth_rate_limiter
    from services.management.app.services.token_denylist import reset_token_denylist

    for name in _REAL_DB_ROUTE_MODULES:
        module = importlib.import_module(f"services.management.app.api.v1.{name}")
        monkeypatch.setattr(module, "db", team_world.db)
    reset_login_throttle()
    reset_auth_rate_limiter()
    reset_token_denylist()
    async with flask_app.test_client() as client:
        yield client
    reset_login_throttle()
    reset_auth_rate_limiter()
    reset_token_denylist()


#: team_world users -> (team key, team role); ``boss``/``loner`` have no team, ``carl`` is org b's.
_TEAM_OF = {"olive": "a1", "adam": "a1", "mia": "a1", "vic": "a1", "nat": "a2", "carl": "b1"}
_TEAM_ROLE_OF = {
    "olive": "owner", "adam": "admin", "mia": "member", "vic": "viewer", "nat": "member",
    "carl": "owner",
}  # fmt: skip


@pytest.fixture
def team_headers(team_world, mint_token):
    """Factory ``team_headers("mia")`` -> bearer headers for a ``team_world`` user.

    The token carries the same team context + scopes a real login would mint (``boss`` is the
    org-a admin; ``loner`` a teamless org-a user; ``carl`` org b's team owner).
    """

    def _make(user: str) -> dict[str, str]:
        w = team_world
        org = "b" if user == "carl" else "a"
        team_key = _TEAM_OF.get(user)
        token = mint_token(
            role="admin" if user == "boss" else "user",
            user_id=w.u[user],
            org_id=w.org[org],
            team_id=w.t[team_key] if team_key else None,
            team_role=_TEAM_ROLE_OF.get(user),
        )
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    return _make


class SharedValkey:
    """One dict seen through BOTH planes' client shapes: management (sync) and proxy (async)."""

    def __init__(self) -> None:
        """Start empty."""
        self.store: dict[str, str] = {}

    def delete(self, *keys: str) -> int:
        """Management's sync ``delete`` (what ``invalidate_key_ids`` calls)."""
        return sum(self.store.pop(k, None) is not None for k in keys)

    async def get(self, key: str) -> str | None:
        """The proxy's async ``get``."""
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        """The proxy's async ``set`` (TTL ignored by the fake)."""
        self.store[key] = value
        return True


@pytest.fixture
def shared_valkey(monkeypatch) -> SharedValkey:
    """A fake Valkey wired in as management's ``redis_client`` and usable as the proxy's client."""
    import services.management.app.extensions as ext_mod

    fake = SharedValkey()
    monkeypatch.setattr(ext_mod, "redis_client", fake)
    return fake


@pytest.fixture
def redis_spy(monkeypatch) -> MagicMock:
    """A recording stand-in for management's Valkey client (``extensions.redis_client``)."""
    import services.management.app.extensions as ext_mod

    client = MagicMock()
    monkeypatch.setattr(ext_mod, "redis_client", client)
    return client


@pytest.fixture(autouse=True)
def _teamless_tokens_for_mock_db_tests(request, monkeypatch):
    """Keep token mints teamless in the legacy MagicMock-DB route tests.

    Those tests wire ONE canned row to every ``db(...).select().first()`` call, so the new
    live team lookup in ``auth._acting_team`` would read that row as a ``team_members`` row.
    Tests that opt in to a real database (``team_world`` / ``real_schema_db``) keep the real
    behaviour. The same canned rows would also confuse the Default-team seeding, so the
    org-create route and the bootstrap seed are stubbed out here too.
    """
    if {"team_world", "real_schema_db"} & set(request.fixturenames):
        return
    monkeypatch.setattr(
        "services.management.app.api.v1.organizations.ensure_default_team", lambda *a, **kw: 1
    )
    monkeypatch.setattr("services.management.app.api.v1.users.seat_new_user", lambda *a, **kw: 1)
    monkeypatch.setattr(
        "services.management.app.services.teams.ensure_default_team", lambda *a, **kw: 1
    )
    monkeypatch.setattr("services.management.app.services.teams.seat_new_user", lambda *a, **kw: 1)
    monkeypatch.setattr(
        "services.management.app.api.v1.auth._acting_team",
        AsyncMock(return_value=(None, None)),
    )
    monkeypatch.setattr(
        "services.management.app.api.v1.keys.resolve_key_team", lambda *a, **kw: None
    )
