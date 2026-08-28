"""Unit tests for the mem0-compatible REST API (`proxy/apps/proxy_server/mem0_api.py`).

Registers `mem0_bp` on a bare Quart app (no full proxy startup — grpc, real
DB, etc.) and injects a fake memory manager plus a monkeypatched
`main.get_current_user` to drive every route: success paths, validation
(400), org-0/cross-org/cross-user auth (403), not-found (404), the
org-scope feature-flag gate, moderator-only delete/clear paths, backend
errors (500/503), and response envelope shape. Fakes are deliberately not
`Mock()` — they only implement the real `MemoryStore`/manager method names
(`store_memory`, `search_memories`, `get_conversation_history`,
`clear_memories`, `write_db.executesql`) so a wrong-method-name bug in the
route code would fail loudly instead of silently matching an auto-mock.
"""

from dataclasses import dataclass, field
from datetime import datetime

import pytest
from quart import Quart

from proxy.apps.proxy_server import main as proxy_main
from proxy.apps.proxy_server.mem0_api import (
    VALID_SCOPES,
    _delete_allowed,
    _is_moderator,
    _resolve_read_scope,
    _resolve_write_scope,
    get_memory_manager,
    mem0_bp,
    set_memory_manager,
)
from shared.auth.rbac import Permission, Role, UserContext
from shared.utils.memory_integration import MemoryEntry

# ---------------------------------------------------------------------------
# Fakes (spec'd to the real method names, never a spec-less Mock)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FakeWriteDB:
    """Fake DAL write handle exposing only `executesql`, matching delete_memory's raw SQL path."""

    select_rows: list = field(default_factory=list)
    raise_on: str | None = None  # "select" | "delete" | None
    calls: list = field(default_factory=list)

    def executesql(self, sql: str, params: tuple) -> list | None:
        """Record the call and either raise, return SELECT rows, or no-op a DELETE."""
        self.calls.append((sql, params))
        if self.raise_on == "select" and "SELECT" in sql:
            raise RuntimeError("db exploded")
        if self.raise_on == "delete" and "DELETE" in sql:
            raise RuntimeError("db exploded")
        if "SELECT" in sql:
            return self.select_rows
        return None


@dataclass(slots=True)
class FakeMemoryStore:
    """Fake MemoryStore recording every call and returning canned results."""

    store_result: bool = True
    search_result: list = field(default_factory=list)
    history_result: list = field(default_factory=list)
    clear_result: bool = True
    write_db: FakeWriteDB = field(default_factory=FakeWriteDB)
    stored_entries: list = field(default_factory=list)
    search_calls: list = field(default_factory=list)
    history_calls: list = field(default_factory=list)
    clear_calls: list = field(default_factory=list)

    async def store_memory(self, entry: MemoryEntry) -> bool:
        """Record the stored entry and return the configured result."""
        self.stored_entries.append(entry)
        return self.store_result

    async def search_memories(self, **kwargs) -> list:
        """Record the search kwargs and return the configured result list."""
        self.search_calls.append(kwargs)
        return self.search_result

    async def get_conversation_history(self, **kwargs) -> list:
        """Record the history kwargs and return the configured result list."""
        self.history_calls.append(kwargs)
        return self.history_result

    async def clear_memories(self, **kwargs) -> bool:
        """Record the clear kwargs and return the configured result."""
        self.clear_calls.append(kwargs)
        return self.clear_result


@dataclass(slots=True)
class FakeManager:
    """Fake WaddleAIMemoryManager exposing only the `memory_store` attribute mem0_api touches."""

    memory_store: FakeMemoryStore


def make_user(
    org_id: int = 7,
    user_id: int = 42,
    permissions: set | None = None,
    role: Role = Role.USER,
) -> UserContext:
    """Build a UserContext fixture-double with sane defaults for org/user scoping tests."""
    return UserContext(
        user_id=user_id,
        username="tester",
        role=role,
        organization_id=org_id,
        managed_orgs=[],
        permissions=set(permissions or set()),
        api_key_id=None,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app() -> Quart:
    """Bare Quart app with only the mem0 blueprint registered."""
    application = Quart(__name__)
    application.register_blueprint(mem0_bp)
    return application


@pytest.fixture
def client(app: Quart):
    """Quart async test client bound to the bare mem0 app."""
    return app.test_client()


@pytest.fixture
def fake_store() -> FakeMemoryStore:
    """A fresh FakeMemoryStore per test."""
    return FakeMemoryStore()


@pytest.fixture
def fake_manager(fake_store: FakeMemoryStore) -> FakeManager:
    """A fresh FakeManager wrapping `fake_store` per test."""
    return FakeManager(memory_store=fake_store)


@pytest.fixture(autouse=True)
def _reset_memory_manager():
    """Ensure the module-level `_memory_manager` global never leaks across tests."""
    yield
    set_memory_manager(None)


@pytest.fixture(autouse=True)
def _clear_org_scope_env(monkeypatch):
    """Guarantee the org-scope feature flag env var is unset unless a test opts in."""
    monkeypatch.delenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", raising=False)
    monkeypatch.delenv("POSTHOG_KEY", raising=False)


@pytest.fixture
def auth_as(monkeypatch):
    """Return a setter that patches `main.get_current_user` to yield a given UserContext."""

    def _set(user: UserContext) -> None:
        async def _fake_get_current_user():
            return user

        monkeypatch.setattr(proxy_main, "get_current_user", _fake_get_current_user)

    return _set


# ---------------------------------------------------------------------------
# Pure-function helper tests (full branch coverage, no HTTP needed)
# ---------------------------------------------------------------------------


class TestResolveWriteScope:
    """Tests for `_resolve_write_scope`."""

    def test_absent_scope_defaults_to_user(self):
        """No 'scope' anywhere in the body means personal ('user')."""
        assert _resolve_write_scope({}) == "user"

    def test_top_level_scope_wins(self):
        """A top-level 'scope' field takes precedence over metadata.scope."""
        assert _resolve_write_scope({"scope": "org", "metadata": {"scope": "user"}}) == "org"

    def test_metadata_scope_is_fallback(self):
        """metadata.scope is used only when the top-level field is absent."""
        assert _resolve_write_scope({"metadata": {"scope": "org"}}) == "org"

    def test_metadata_none_does_not_raise(self):
        """A None metadata value falls back to {} instead of raising."""
        assert _resolve_write_scope({"metadata": None}) == "user"

    def test_invalid_scope_returns_none(self):
        """An unrecognized scope value is rejected, not silently accepted."""
        assert _resolve_write_scope({"scope": "bogus"}) is None


class TestResolveReadScope:
    """Tests for `_resolve_read_scope`."""

    def test_absent_returns_merged_view(self):
        """No filter means the internal 'all' (merged) sentinel."""
        assert _resolve_read_scope(None) == "all"
        assert _resolve_read_scope("") == "all"

    def test_valid_scope_passthrough(self):
        """A valid scope value passes through unchanged."""
        assert _resolve_read_scope("org") == "org"

    def test_invalid_scope_returns_none(self):
        """An unrecognized scope value is rejected."""
        assert _resolve_read_scope("bogus") is None


class TestIsModerator:
    """Tests for `_is_moderator`, which must check both enum and string forms."""

    def test_no_permissions_is_false(self):
        """A user with no permissions is never a moderator."""
        assert _is_moderator(make_user(permissions=set())) is False

    def test_enum_permission_is_true(self):
        """MEMORY_MODERATE as an enum member (direct-auth path) is recognized."""
        assert _is_moderator(make_user(permissions={Permission.MEMORY_MODERATE})) is True

    def test_string_permission_is_true(self):
        """MEMORY_MODERATE as a plain string (claims path) is recognized."""
        assert _is_moderator(make_user(permissions={"memory:moderate"})) is True


class TestDeleteAllowed:
    """Tests for `_delete_allowed`."""

    def test_personal_row_owner_allowed(self):
        """A personal row's owner may always delete it."""
        allowed, err = _delete_allowed("user", 0, 42, 42, False)
        assert allowed is True
        assert err == ""

    def test_personal_row_non_owner_denied(self):
        """A personal row may not be deleted by anyone but its owner, moderator or not."""
        allowed, err = _delete_allowed("user", 0, 99, 42, True)
        assert allowed is False
        assert err == "user mismatch"

    def test_org_row_author_allowed(self):
        """An org row's author may delete it without moderator permission."""
        allowed, err = _delete_allowed("org", 42, 42, 42, False)
        assert allowed is True

    def test_org_row_moderator_allowed(self):
        """A moderator may delete an org row they did not author."""
        allowed, err = _delete_allowed("org", 1, 1, 42, True)
        assert allowed is True

    def test_org_row_non_author_non_moderator_denied(self):
        """Neither authoring nor moderating denies an org-row delete."""
        allowed, err = _delete_allowed("org", 1, 1, 42, False)
        assert allowed is False
        assert err == "not memory author"


# ---------------------------------------------------------------------------
# get_memory_manager / set_memory_manager (503 when uninitialized)
# ---------------------------------------------------------------------------


class TestMemoryManagerInjection:
    """Tests for the module-level manager injection helpers."""

    async def test_get_memory_manager_aborts_503_when_unset(self, app: Quart):
        """get_memory_manager raises a 503 HTTPException before any manager is injected."""
        from werkzeug.exceptions import ServiceUnavailable

        async with app.app_context():
            with pytest.raises(ServiceUnavailable):
                get_memory_manager()

    def test_set_then_get_returns_injected_manager(self, fake_manager: FakeManager):
        """After set_memory_manager, get_memory_manager returns the same instance."""
        set_memory_manager(fake_manager)
        assert get_memory_manager() is fake_manager


async def test_route_returns_503_when_manager_not_initialized(client):
    """Every route calls get_memory_manager() first; an uninitialized manager 503s pre-auth."""
    resp = await client.post("/mem0/memories", json={"messages": []})
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# POST /mem0/memories (add_memories)
# ---------------------------------------------------------------------------


async def test_add_memories_success_returns_stored_count(client, auth_as, fake_manager):
    """A well-formed add returns stored=1 and echoes back user_id/session_id."""
    set_memory_manager(fake_manager)
    auth_as(make_user(org_id=7, user_id=42))

    resp = await client.post(
        "/mem0/memories",
        json={"messages": [{"role": "user", "content": "hello there"}]},
    )

    assert resp.status_code == 200
    data = await resp.get_json()
    assert data == {"status": "success", "stored": 1, "user_id": "0", "session_id": ""}
    stored = fake_manager.memory_store.stored_entries[0]
    assert stored.user_id == 42
    assert stored.organization_id == 7
    assert stored.scope_type == "user"
    assert stored.author_user_id == 42


async def test_add_memories_skips_blank_content_messages(client, auth_as, fake_manager):
    """A message whose content is whitespace-only is not persisted."""
    set_memory_manager(fake_manager)
    auth_as(make_user())

    resp = await client.post(
        "/mem0/memories", json={"messages": [{"role": "user", "content": "   "}]}
    )

    assert resp.status_code == 200
    data = await resp.get_json()
    assert data["stored"] == 0
    assert fake_manager.memory_store.stored_entries == []


async def test_add_memories_store_failure_not_counted(client, auth_as, fake_manager):
    """A store_memory() False result is not counted as stored."""
    fake_manager.memory_store.store_result = False
    set_memory_manager(fake_manager)
    auth_as(make_user())

    resp = await client.post(
        "/mem0/memories", json={"messages": [{"role": "user", "content": "hi"}]}
    )

    assert (await resp.get_json())["stored"] == 0


async def test_add_memories_run_id_fallback_session_id(client, auth_as, fake_manager):
    """session_id falls back to run_id when agent_id is absent."""
    set_memory_manager(fake_manager)
    auth_as(make_user())

    resp = await client.post(
        "/mem0/memories",
        json={"messages": [{"role": "user", "content": "hi"}], "run_id": "run-1"},
    )

    assert (await resp.get_json())["session_id"] == "run-1"


async def test_add_memories_rejects_org_zero_token(client, auth_as, fake_manager):
    """A caller whose token carries org_id=0 is rejected before any body processing."""
    set_memory_manager(fake_manager)
    auth_as(make_user(org_id=0))

    resp = await client.post("/mem0/memories", json={"messages": []})

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "no valid organization"


async def test_add_memories_missing_body_is_400(client, auth_as, fake_manager):
    """A POST with no JSON body at all is rejected with 400."""
    set_memory_manager(fake_manager)
    auth_as(make_user())

    resp = await client.post("/mem0/memories")

    assert resp.status_code == 400
    assert b"Request body required" in await resp.get_data()


async def test_add_memories_cross_org_rejected(client, auth_as, fake_manager):
    """A body organization_id that disagrees with the token's org is a 403."""
    set_memory_manager(fake_manager)
    auth_as(make_user(org_id=7))

    resp = await client.post("/mem0/memories", json={"messages": [], "organization_id": 999})

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "organization mismatch"


async def test_add_memories_cross_user_rejected(client, auth_as, fake_manager):
    """A body user_id that disagrees with the token's user is a 403."""
    set_memory_manager(fake_manager)
    auth_as(make_user(user_id=42))

    resp = await client.post("/mem0/memories", json={"messages": [], "user_id": "99"})

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "user mismatch"


async def test_add_memories_non_numeric_user_id_treated_as_zero(client, auth_as, fake_manager):
    """A non-numeric user_id falls back to 0 instead of raising, then fails the mismatch check."""
    set_memory_manager(fake_manager)
    auth_as(make_user(user_id=42))

    resp = await client.post("/mem0/memories", json={"messages": [], "user_id": "not-a-number"})

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "user mismatch"


async def test_add_memories_non_numeric_org_id_treated_as_zero(client, auth_as, fake_manager):
    """A non-numeric organization_id falls back to 0 instead of raising."""
    set_memory_manager(fake_manager)
    auth_as(make_user(org_id=7))

    resp = await client.post(
        "/mem0/memories", json={"messages": [], "organization_id": "not-a-number"}
    )

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "organization mismatch"


async def test_add_memories_matching_organization_id_is_accepted(client, auth_as, fake_manager):
    """A body organization_id equal to the token's org passes through without a 403."""
    set_memory_manager(fake_manager)
    auth_as(make_user(org_id=7, user_id=42))

    resp = await client.post(
        "/mem0/memories",
        json={"messages": [{"role": "user", "content": "hi"}], "organization_id": 7},
    )

    assert resp.status_code == 200


async def test_add_memories_invalid_scope_is_400(client, auth_as, fake_manager):
    """An unrecognized scope value in the body is rejected with 400."""
    set_memory_manager(fake_manager)
    auth_as(make_user())

    resp = await client.post("/mem0/memories", json={"messages": [], "scope": "bogus"})

    assert resp.status_code == 400
    assert (await resp.get_json())["error"] == "invalid scope"


async def test_add_memories_org_scope_denied_when_flag_off(client, auth_as, fake_manager):
    """Org-scoped writes are rejected while the org-scope flag defaults OFF."""
    set_memory_manager(fake_manager)
    auth_as(make_user())

    resp = await client.post(
        "/mem0/memories", json={"messages": [{"role": "user", "content": "hi"}], "scope": "org"}
    )

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "organization memory scope not enabled"
    assert fake_manager.memory_store.stored_entries == []


async def test_add_memories_org_scope_allowed_when_flag_on(
    client, auth_as, fake_manager, monkeypatch
):
    """Org-scoped writes succeed once the org-scope flag is enabled via env override."""
    monkeypatch.setenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", "true")
    set_memory_manager(fake_manager)
    auth_as(make_user(user_id=42))

    resp = await client.post(
        "/mem0/memories", json={"messages": [{"role": "user", "content": "hi"}], "scope": "org"}
    )

    assert resp.status_code == 200
    stored = fake_manager.memory_store.stored_entries[0]
    assert stored.scope_type == "org"
    assert stored.author_user_id == 42


# ---------------------------------------------------------------------------
# POST /mem0/memories/search (search_memories)
# ---------------------------------------------------------------------------


def _entry(**overrides) -> MemoryEntry:
    """Build a MemoryEntry with sane defaults, overridable per test."""
    defaults = dict(
        id="m1",
        user_id=42,
        organization_id=7,
        session_id="s1",
        content="hello world",
        metadata={"role": "user"},
        embedding=None,
        created_at=datetime(2026, 1, 1, 12, 0, 0),
        relevance_score=0.87,
        scope_type="user",
        author_user_id=42,
    )
    defaults.update(overrides)
    return MemoryEntry(**defaults)


async def test_search_memories_respects_limit_param(client, auth_as, fake_manager):
    """The 'limit' body field is forwarded to the store's search call untouched."""
    set_memory_manager(fake_manager)
    auth_as(make_user())

    resp = await client.post("/mem0/memories/search", json={"query": "hi", "limit": 3})

    assert resp.status_code == 200
    assert fake_manager.memory_store.search_calls[0]["limit"] == 3


async def test_search_memories_maps_result_shape(client, auth_as, fake_manager):
    """Search results map every MemoryEntry field, with fallbacks for None fields."""
    fake_manager.memory_store.search_result = [
        _entry(id="m1", created_at=datetime(2026, 1, 1), author_user_id=42),
        _entry(id="m2", created_at=None, author_user_id=0, user_id=42, scope_type="org"),
    ]
    set_memory_manager(fake_manager)
    auth_as(make_user(user_id=42))

    resp = await client.post("/mem0/memories/search", json={"query": "hi"})

    data = await resp.get_json()
    assert data["total"] == 2
    first, second = data["results"]
    assert first["created_at"] == datetime(2026, 1, 1).isoformat()
    assert first["author_user_id"] == "42"
    assert second["created_at"] is None
    # author_user_id=0 is falsy -> fallback to entry.user_id
    assert second["author_user_id"] == "42"
    assert second["scope"] == "org"


async def test_search_memories_rejects_org_zero_token(client, auth_as, fake_manager):
    """org_id=0 tokens are rejected on the search route too."""
    set_memory_manager(fake_manager)
    auth_as(make_user(org_id=0))

    resp = await client.post("/mem0/memories/search", json={"query": "hi"})

    assert resp.status_code == 403


async def test_search_memories_missing_body_is_400(client, auth_as, fake_manager):
    """No JSON body on search is a 400."""
    set_memory_manager(fake_manager)
    auth_as(make_user())

    resp = await client.post("/mem0/memories/search")

    assert resp.status_code == 400


async def test_search_memories_cross_org_rejected(client, auth_as, fake_manager):
    """A cross-org search request is rejected with 403."""
    set_memory_manager(fake_manager)
    auth_as(make_user(org_id=7))

    resp = await client.post("/mem0/memories/search", json={"query": "hi", "organization_id": 999})

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "organization mismatch"


async def test_search_memories_cross_user_rejected(client, auth_as, fake_manager):
    """A cross-user search request is rejected with 403."""
    set_memory_manager(fake_manager)
    auth_as(make_user(user_id=42))

    resp = await client.post("/mem0/memories/search", json={"query": "hi", "user_id": "99"})

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "user mismatch"


async def test_search_memories_non_numeric_user_id_treated_as_zero(client, auth_as, fake_manager):
    """A non-numeric user_id falls back to 0 instead of raising, then fails the mismatch check."""
    set_memory_manager(fake_manager)
    auth_as(make_user(user_id=42))

    resp = await client.post(
        "/mem0/memories/search", json={"query": "hi", "user_id": "not-a-number"}
    )

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "user mismatch"


async def test_search_memories_non_numeric_org_id_treated_as_zero(client, auth_as, fake_manager):
    """A non-numeric organization_id falls back to 0 instead of raising."""
    set_memory_manager(fake_manager)
    auth_as(make_user(org_id=7))

    resp = await client.post(
        "/mem0/memories/search", json={"query": "hi", "organization_id": "not-a-number"}
    )

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "organization mismatch"


async def test_search_memories_matching_organization_id_is_accepted(client, auth_as, fake_manager):
    """An organization_id equal to the token's org passes through without a 403."""
    set_memory_manager(fake_manager)
    auth_as(make_user(org_id=7))

    resp = await client.post("/mem0/memories/search", json={"query": "hi", "organization_id": 7})

    assert resp.status_code == 200


async def test_search_memories_invalid_scope_is_400(client, auth_as, fake_manager):
    """An unrecognized scope filter is a 400."""
    set_memory_manager(fake_manager)
    auth_as(make_user())

    resp = await client.post("/mem0/memories/search", json={"query": "hi", "scope": "bogus"})

    assert resp.status_code == 400
    assert (await resp.get_json())["error"] == "invalid scope"


# ---------------------------------------------------------------------------
# GET /mem0/memories (list_memories)
# ---------------------------------------------------------------------------


async def test_list_memories_forwards_limit_query_param(client, auth_as, fake_manager):
    """The 'limit' query param is parsed to int and forwarded to the store."""
    fake_manager.memory_store.history_result = [_entry()]
    set_memory_manager(fake_manager)
    auth_as(make_user())

    resp = await client.get("/mem0/memories?limit=5")

    assert resp.status_code == 200
    assert fake_manager.memory_store.history_calls[0]["limit"] == 5
    data = await resp.get_json()
    assert data["total"] == 1
    assert data["memories"][0]["id"] == "m1"


async def test_list_memories_default_limit_is_twenty(client, auth_as, fake_manager):
    """Without a 'limit' query param, the default of 20 is used."""
    set_memory_manager(fake_manager)
    auth_as(make_user())

    await client.get("/mem0/memories")

    assert fake_manager.memory_store.history_calls[0]["limit"] == 20


async def test_list_memories_rejects_org_zero_token(client, auth_as, fake_manager):
    """org_id=0 tokens are rejected on the list route too."""
    set_memory_manager(fake_manager)
    auth_as(make_user(org_id=0))

    resp = await client.get("/mem0/memories")

    assert resp.status_code == 403


async def test_list_memories_cross_org_rejected(client, auth_as, fake_manager):
    """A cross-org organization_id query param is rejected with 403."""
    set_memory_manager(fake_manager)
    auth_as(make_user(org_id=7))

    resp = await client.get("/mem0/memories?organization_id=999")

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "organization mismatch"


async def test_list_memories_cross_user_rejected(client, auth_as, fake_manager):
    """A cross-user user_id query param is rejected with 403."""
    set_memory_manager(fake_manager)
    auth_as(make_user(user_id=42))

    resp = await client.get("/mem0/memories?user_id=99")

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "user mismatch"


async def test_list_memories_non_numeric_user_id_treated_as_zero(client, auth_as, fake_manager):
    """A non-numeric user_id query param falls back to 0, then fails the mismatch check."""
    set_memory_manager(fake_manager)
    auth_as(make_user(user_id=42))

    resp = await client.get("/mem0/memories?user_id=not-a-number")

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "user mismatch"


async def test_list_memories_non_numeric_org_id_treated_as_zero(client, auth_as, fake_manager):
    """A non-numeric organization_id query param falls back to 0 instead of raising."""
    set_memory_manager(fake_manager)
    auth_as(make_user(org_id=7))

    resp = await client.get("/mem0/memories?organization_id=not-a-number")

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "organization mismatch"


async def test_list_memories_matching_organization_id_is_accepted(client, auth_as, fake_manager):
    """An organization_id query param equal to the token's org passes through without a 403."""
    set_memory_manager(fake_manager)
    auth_as(make_user(org_id=7))

    resp = await client.get("/mem0/memories?organization_id=7")

    assert resp.status_code == 200


async def test_list_memories_invalid_scope_is_400(client, auth_as, fake_manager):
    """An unrecognized scope filter query param is a 400."""
    set_memory_manager(fake_manager)
    auth_as(make_user())

    resp = await client.get("/mem0/memories?scope=bogus")

    assert resp.status_code == 400
    assert (await resp.get_json())["error"] == "invalid scope"


# ---------------------------------------------------------------------------
# DELETE /mem0/memories/<id> (delete_memory)
# ---------------------------------------------------------------------------


async def test_delete_memory_success_personal_owner(client, auth_as, fake_manager):
    """The owner of a personal memory row may delete it."""
    fake_manager.memory_store.write_db.select_rows = [("user", None, 42)]
    set_memory_manager(fake_manager)
    auth_as(make_user(user_id=42))

    resp = await client.delete("/mem0/memories/5")

    assert resp.status_code == 200
    assert await resp.get_json() == {"status": "deleted", "id": "5"}


async def test_delete_memory_not_found_returns_404(client, auth_as, fake_manager):
    """A memory ID with no matching row is a 404, not a 403 or 500."""
    fake_manager.memory_store.write_db.select_rows = []
    set_memory_manager(fake_manager)
    auth_as(make_user())

    resp = await client.delete("/mem0/memories/999")

    assert resp.status_code == 404
    assert (await resp.get_json())["error"] == "memory not found"


async def test_delete_memory_personal_row_wrong_owner_is_403(client, auth_as, fake_manager):
    """A personal row owned by a different user is not deletable, moderator or not."""
    fake_manager.memory_store.write_db.select_rows = [("user", None, 999)]
    set_memory_manager(fake_manager)
    auth_as(make_user(user_id=42, permissions={Permission.MEMORY_MODERATE}))

    resp = await client.delete("/mem0/memories/5")

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "user mismatch"


async def test_delete_memory_org_row_author_allowed(client, auth_as, fake_manager):
    """The author of an org row may delete it without moderator permission."""
    fake_manager.memory_store.write_db.select_rows = [("org", 42, 42)]
    set_memory_manager(fake_manager)
    auth_as(make_user(user_id=42))

    resp = await client.delete("/mem0/memories/5")

    assert resp.status_code == 200


async def test_delete_memory_org_row_non_author_denied(client, auth_as, fake_manager):
    """A non-author, non-moderator caller cannot delete an org row."""
    fake_manager.memory_store.write_db.select_rows = [("org", 1, 1)]
    set_memory_manager(fake_manager)
    auth_as(make_user(user_id=42))

    resp = await client.delete("/mem0/memories/5")

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "not memory author"


async def test_delete_memory_org_row_moderator_allowed(client, auth_as, fake_manager):
    """A memory:moderate holder may delete any org row, authored or not."""
    fake_manager.memory_store.write_db.select_rows = [("org", 1, 1)]
    set_memory_manager(fake_manager)
    auth_as(make_user(user_id=42, permissions={Permission.MEMORY_MODERATE}))

    resp = await client.delete("/mem0/memories/5")

    assert resp.status_code == 200


async def test_delete_memory_rejects_org_zero_token(client, auth_as, fake_manager):
    """org_id=0 tokens are rejected on the delete route too."""
    set_memory_manager(fake_manager)
    auth_as(make_user(org_id=0))

    resp = await client.delete("/mem0/memories/5")

    assert resp.status_code == 403


async def test_delete_memory_cross_org_rejected(client, auth_as, fake_manager):
    """A cross-org organization_id query param is rejected before hitting the store."""
    set_memory_manager(fake_manager)
    auth_as(make_user(org_id=7))

    resp = await client.delete("/mem0/memories/5?organization_id=999")

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "organization mismatch"
    assert fake_manager.memory_store.write_db.calls == []


async def test_delete_memory_cross_user_rejected(client, auth_as, fake_manager):
    """A cross-user user_id query param is rejected before hitting the store."""
    set_memory_manager(fake_manager)
    auth_as(make_user(user_id=42))

    resp = await client.delete("/mem0/memories/5?user_id=99")

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "user mismatch"


async def test_delete_memory_non_numeric_user_id_treated_as_zero(client, auth_as, fake_manager):
    """A non-numeric user_id query param falls back to 0, then fails the mismatch check."""
    set_memory_manager(fake_manager)
    auth_as(make_user(user_id=42))

    resp = await client.delete("/mem0/memories/5?user_id=not-a-number")

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "user mismatch"


async def test_delete_memory_non_numeric_org_id_treated_as_zero(client, auth_as, fake_manager):
    """A non-numeric organization_id query param falls back to 0 instead of raising."""
    set_memory_manager(fake_manager)
    auth_as(make_user(org_id=7))

    resp = await client.delete("/mem0/memories/5?organization_id=not-a-number")

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "organization mismatch"


async def test_delete_memory_matching_organization_id_is_accepted(client, auth_as, fake_manager):
    """An organization_id query param equal to the token's org passes through without a 403."""
    fake_manager.memory_store.write_db.select_rows = [("user", None, 42)]
    set_memory_manager(fake_manager)
    auth_as(make_user(org_id=7, user_id=42))

    resp = await client.delete("/mem0/memories/5?organization_id=7")

    assert resp.status_code == 200


async def test_delete_memory_backend_error_maps_to_500(client, auth_as, fake_manager):
    """A raw-SQL failure is caught, logged, and mapped to a 500 (not a raw traceback)."""
    fake_manager.memory_store.write_db.raise_on = "select"
    set_memory_manager(fake_manager)
    auth_as(make_user())

    resp = await client.delete("/mem0/memories/5")

    assert resp.status_code == 500
    assert b"Failed to delete memory" in await resp.get_data()


async def test_delete_memory_non_integer_id_maps_to_500(client, auth_as, fake_manager):
    """A non-numeric memory_id raises ValueError inside the SQL call, caught as a 500."""
    set_memory_manager(fake_manager)
    auth_as(make_user())

    resp = await client.delete("/mem0/memories/not-a-number")

    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# DELETE /mem0/memories (clear_memories)
# ---------------------------------------------------------------------------


async def test_clear_memories_success_default_scope(client, auth_as, fake_manager):
    """A bare clear request defaults to scope='user', org_all=False."""
    set_memory_manager(fake_manager)
    auth_as(make_user(user_id=42))

    resp = await client.delete("/mem0/memories")

    assert resp.status_code == 200
    assert await resp.get_json() == {"status": "cleared", "user_id": "0"}
    call = fake_manager.memory_store.clear_calls[0]
    assert call["scope"] == "user"
    assert call["org_all"] is False
    assert call["session_id"] is None


async def test_clear_memories_rejects_org_zero_token(client, auth_as, fake_manager):
    """org_id=0 tokens are rejected on the clear route too."""
    set_memory_manager(fake_manager)
    auth_as(make_user(org_id=0))

    resp = await client.delete("/mem0/memories")

    assert resp.status_code == 403


async def test_clear_memories_cross_org_rejected(client, auth_as, fake_manager):
    """A cross-org organization_id query param is rejected with 403."""
    set_memory_manager(fake_manager)
    auth_as(make_user(org_id=7))

    resp = await client.delete("/mem0/memories?organization_id=999")

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "organization mismatch"


async def test_clear_memories_cross_user_rejected(client, auth_as, fake_manager):
    """A cross-user user_id query param is rejected with 403."""
    set_memory_manager(fake_manager)
    auth_as(make_user(user_id=42))

    resp = await client.delete("/mem0/memories?user_id=99")

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "user mismatch"


async def test_clear_memories_non_numeric_user_id_treated_as_zero(client, auth_as, fake_manager):
    """A non-numeric user_id query param falls back to 0, then fails the mismatch check."""
    set_memory_manager(fake_manager)
    auth_as(make_user(user_id=42))

    resp = await client.delete("/mem0/memories?user_id=not-a-number")

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "user mismatch"


async def test_clear_memories_non_numeric_org_id_treated_as_zero(client, auth_as, fake_manager):
    """A non-numeric organization_id query param falls back to 0 instead of raising."""
    set_memory_manager(fake_manager)
    auth_as(make_user(org_id=7))

    resp = await client.delete("/mem0/memories?organization_id=not-a-number")

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "organization mismatch"


async def test_clear_memories_matching_organization_id_is_accepted(client, auth_as, fake_manager):
    """An organization_id query param equal to the token's org passes through without a 403."""
    set_memory_manager(fake_manager)
    auth_as(make_user(org_id=7))

    resp = await client.delete("/mem0/memories?organization_id=7")

    assert resp.status_code == 200


async def test_clear_memories_invalid_scope_is_400(client, auth_as, fake_manager):
    """An unrecognized scope query param is a 400, distinct from the 'absent' default."""
    set_memory_manager(fake_manager)
    auth_as(make_user())

    resp = await client.delete("/mem0/memories?scope=bogus")

    assert resp.status_code == 400
    assert (await resp.get_json())["error"] == "invalid scope"


async def test_clear_memories_org_all_requires_moderator(client, auth_as, fake_manager):
    """scope=org&all=true is refused for a non-moderator caller."""
    set_memory_manager(fake_manager)
    auth_as(make_user(permissions=set()))

    resp = await client.delete("/mem0/memories?scope=org&all=true")

    assert resp.status_code == 403
    assert (await resp.get_json())["error"] == "memory moderation permission required"
    assert fake_manager.memory_store.clear_calls == []


async def test_clear_memories_org_all_allowed_for_moderator(client, auth_as, fake_manager):
    """scope=org&all=true succeeds for a memory:moderate holder."""
    set_memory_manager(fake_manager)
    auth_as(make_user(permissions={Permission.MEMORY_MODERATE}))

    resp = await client.delete("/mem0/memories?scope=org&all=true")

    assert resp.status_code == 200
    call = fake_manager.memory_store.clear_calls[0]
    assert call["scope"] == "org"
    assert call["org_all"] is True


async def test_clear_memories_org_author_scoped_no_moderator_needed(client, auth_as, fake_manager):
    """scope=org without all=true clears only the caller's own rows -- no moderator needed."""
    set_memory_manager(fake_manager)
    auth_as(make_user(permissions=set()))

    resp = await client.delete("/mem0/memories?scope=org")

    assert resp.status_code == 200
    call = fake_manager.memory_store.clear_calls[0]
    assert call["scope"] == "org"
    assert call["org_all"] is False


async def test_clear_memories_backend_failure_maps_to_500(client, auth_as, fake_manager):
    """A False result from the store's clear_memories() is mapped to a 500."""
    fake_manager.memory_store.clear_result = False
    set_memory_manager(fake_manager)
    auth_as(make_user())

    resp = await client.delete("/mem0/memories")

    assert resp.status_code == 500
    assert b"Failed to clear memories" in await resp.get_data()


# ---------------------------------------------------------------------------
# Cross-route: VALID_SCOPES sanity (guards the wire contract used above)
# ---------------------------------------------------------------------------


def test_valid_scopes_are_user_and_org_only():
    """VALID_SCOPES is the wire contract every scope-validation test above relies on."""
    assert VALID_SCOPES == ("user", "org")
