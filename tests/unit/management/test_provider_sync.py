"""Unit tests for ProviderSyncService: Ollama model-route sync to AILB.

Covers the sync/diff loop, per-model route conversion, per-deployment error
isolation (`sync_all_ollama_deployments`), route removal, and status lookup.
Uses small hand-written fakes for the PyDAL-style `db` chain and the AILB
gRPC client rather than spec-less `MagicMock()` -- see `_FakeDB` and
`_FakeAILBClient` below -- so behaviour (rows inserted/updated/deleted, calls
made to AILB) can be asserted directly instead of just "no exception".
"""

from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from services.management.app.grpc.client import RouteConfig
from services.management.app.services.provider_sync import (
    ProviderSyncService,
    SyncResult,
    SyncStatus,
)

# ---------------------------------------------------------------------------
# Hand-written fakes: PyDAL-style `db` chain
# ---------------------------------------------------------------------------


class _Row(SimpleNamespace):
    """A mutable, attribute-addressable fake DB row."""


class _Predicate:
    """A composable filter over one fake table's rows."""

    def __init__(self, table_name: str, fn: Any) -> None:
        """Bind this predicate to the table it filters and its row test."""
        self.table_name = table_name
        self.fn = fn

    def __and__(self, other: "_Predicate") -> "_Predicate":
        """Combine two same-table predicates with logical AND."""
        assert self.table_name == other.table_name, "cross-table AND not used by provider_sync"
        return _Predicate(self.table_name, lambda row: self.fn(row) and other.fn(row))


class _FakeField:
    """A fake `db.<table>.<field>` column reference, building `_Predicate`s."""

    def __init__(self, table_name: str, field_name: str) -> None:
        """Bind this field to its owning table and column name."""
        self._table_name = table_name
        self._field_name = field_name

    def __eq__(self, other: Any) -> _Predicate:  # type: ignore[override]
        """Build an equality predicate."""
        name = self._field_name
        return _Predicate(self._table_name, lambda row: getattr(row, name) == other)

    def __gt__(self, other: Any) -> _Predicate:
        """Build a greater-than predicate."""
        name = self._field_name
        return _Predicate(self._table_name, lambda row: getattr(row, name) > other)

    def belongs(self, values: Any) -> _Predicate:
        """Build a membership predicate, mirroring PyDAL's `.belongs()`."""
        name = self._field_name
        return _Predicate(self._table_name, lambda row: getattr(row, name) in values)


class _SelectResult:
    """Result of `.select()`: iterable, sized, and `.first()`-able."""

    def __init__(self, rows: list[_Row]) -> None:
        """Snapshot the matching rows at select time."""
        self._rows = rows

    def first(self) -> _Row | None:
        """Return the first matching row, or None."""
        return self._rows[0] if self._rows else None

    def __iter__(self):
        """Iterate the matching rows (fresh each call, like a real select)."""
        return iter(self._rows)

    def __len__(self) -> int:
        """Return the number of matching rows."""
        return len(self._rows)


class _FakeQuerySet:
    """Result of `db(predicate)`: supports select/count/update/delete."""

    def __init__(self, db: "_FakeDB", predicate: _Predicate) -> None:
        """Bind this query set to its owning fake db and predicate."""
        self._db = db
        self._predicate = predicate

    def _matches(self) -> list[_Row]:
        return [r for r in self._db.table(self._predicate.table_name) if self._predicate.fn(r)]

    def select(self) -> _SelectResult:
        """Return matching rows as a `_SelectResult`."""
        return _SelectResult(self._matches())

    def count(self) -> int:
        """Return the number of matching rows."""
        return len(self._matches())

    def update(self, **kwargs: Any) -> int:
        """Apply field updates to every matching row; return the count updated."""
        rows = self._matches()
        for row in rows:
            for key, value in kwargs.items():
                setattr(row, key, value)
        return len(rows)

    def delete(self) -> int:
        """Remove every matching row from its table; return the count deleted."""
        rows = self._matches()
        table = self._db.table(self._predicate.table_name)
        for row in rows:
            table.remove(row)
        return len(rows)


class _FakeTable:
    """A fake `db.<table_name>` accessor, yielding `_FakeField`s for any column."""

    def __init__(self, db: "_FakeDB", name: str) -> None:
        """Bind this table accessor to its owning fake db and table name."""
        self._db = db
        self._name = name

    def __getattr__(self, field_name: str) -> _FakeField:
        return _FakeField(self._name, field_name)

    def insert(self, **kwargs: Any) -> int:
        """Insert a new row, auto-assigning an id; return the new id."""
        row_id = self._db.next_id(self._name)
        self._db.table(self._name).append(_Row(id=row_id, **kwargs))
        return row_id


class _FakeDB:
    """Hand-written fake of the PyDAL-style `db` chain ProviderSyncService uses.

    Supports exactly the subset of the API the service touches:
    `db.<table>.<field>` comparisons/`.belongs()`, `db(predicate)` returning
    a query set with `.select()/.count()/.update()/.delete()`, table
    `.insert()`, and `db.commit()`.
    """

    def __init__(self) -> None:
        """Seed empty tables for the three tables provider_sync touches."""
        self._tables: dict[str, list[_Row]] = {
            "ollama_deployments": [],
            "ollama_models": [],
            "ollama_model_routes": [],
        }
        self._next_ids: dict[str, int] = dict.fromkeys(self._tables, 0)
        self.ollama_deployments = _FakeTable(self, "ollama_deployments")
        self.ollama_models = _FakeTable(self, "ollama_models")
        self.ollama_model_routes = _FakeTable(self, "ollama_model_routes")
        self.commit_calls = 0

    def table(self, name: str) -> list[_Row]:
        """Return the live row list backing a table (for filtering/mutation)."""
        return self._tables[name]

    def next_id(self, name: str) -> int:
        """Allocate the next auto-increment id for a table."""
        self._next_ids[name] += 1
        return self._next_ids[name]

    def seed(self, table_name: str, **fields: Any) -> _Row:
        """Insert a pre-built row directly (bypassing field validation), for setup."""
        row_id = fields.pop("id", None)
        if row_id is None:
            row_id = self.next_id(table_name)
        else:
            self._next_ids[table_name] = max(self._next_ids[table_name], row_id)
        row = _Row(id=row_id, **fields)
        self._tables[table_name].append(row)
        return row

    def commit(self) -> None:
        """Record that a commit was requested."""
        self.commit_calls += 1

    def __call__(self, predicate: _Predicate) -> _FakeQuerySet:
        """Start a query: `db(db.table.field == value)`."""
        return _FakeQuerySet(self, predicate)


# ---------------------------------------------------------------------------
# Hand-written fake: AILB gRPC client
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _FakeAILBClient:
    """Hand-written fake of AILBModuleClient covering only the methods used here."""

    connected: bool = True
    update_result: dict[str, Any] = field(default_factory=lambda: {"success": True})
    fail_route_prefix: str | None = None
    raise_on_delete: Exception | None = None
    update_calls: list[tuple[list[RouteConfig], str]] = field(default_factory=list)
    delete_calls: list[tuple[str, str]] = field(default_factory=list)

    def is_connected(self) -> bool:
        """Report the configured connection state."""
        return self.connected

    def update_routes(
        self, routes: list[RouteConfig], instance_id: str = "", replace_all: bool = False
    ) -> dict[str, Any]:
        """Record the call; raise if any route matches `fail_route_prefix`."""
        self.update_calls.append((list(routes), instance_id))
        if self.fail_route_prefix and any(
            r.route_id.startswith(self.fail_route_prefix) for r in routes
        ):
            raise ConnectionError(f"AILB unreachable for routes under {self.fail_route_prefix!r}")
        return self.update_result

    def delete_route(self, route_id: str, instance_id: str = "") -> bool:
        """Record the call; raise if `raise_on_delete` is configured."""
        self.delete_calls.append((route_id, instance_id))
        if self.raise_on_delete is not None:
            raise self.raise_on_delete
        return True


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _seed_deployment(db: _FakeDB, *, deployment_id: int = 1, status: str = "running") -> _Row:
    """Seed one ollama_deployments row with sane defaults."""
    return db.seed(
        "ollama_deployments",
        id=deployment_id,
        name=f"deployment-{deployment_id}",
        endpoint_url="http://localhost:11434",
        status=status,
    )


def _seed_model(
    db: _FakeDB,
    *,
    model_id: int,
    deployment_id: int,
    model_name: str = "llama3",
    model_tag: str | None = "8b",
) -> _Row:
    """Seed one ollama_models row belonging to a deployment."""
    return db.seed(
        "ollama_models",
        id=model_id,
        deployment_id=deployment_id,
        model_name=model_name,
        model_tag=model_tag,
    )


# ---------------------------------------------------------------------------
# SyncResult
# ---------------------------------------------------------------------------


class TestSyncResult:
    """Tests for the SyncResult dataclass's timestamp default."""

    def test_defaults_timestamp_to_now(self) -> None:
        """No timestamp supplied -> __post_init__ fills one in."""
        result = SyncResult(success=True, provider_id=1)
        assert isinstance(result.timestamp, datetime)

    def test_keeps_supplied_timestamp(self) -> None:
        """A supplied timestamp is left untouched."""
        ts = datetime(2025, 6, 1, 0, 0, 0)
        result = SyncResult(success=True, provider_id=1, timestamp=ts)
        assert result.timestamp is ts


# ---------------------------------------------------------------------------
# set_ailb_client / set_instance_id
# ---------------------------------------------------------------------------


class TestSetters:
    """Tests for the two plain setters."""

    def test_set_ailb_client(self) -> None:
        """The AILB client reference is swapped in."""
        service = ProviderSyncService(_FakeDB())
        client = _FakeAILBClient()
        service.set_ailb_client(client)
        assert service.ailb_client is client

    def test_set_instance_id(self) -> None:
        """The instance id is stored and used on subsequent AILB calls."""
        db = _FakeDB()
        _seed_deployment(db)
        _seed_model(db, model_id=1, deployment_id=1)
        client = _FakeAILBClient()
        service = ProviderSyncService(db, client)
        service.set_instance_id("ailb-west-1")

        service.sync_ollama_deployment(1)
        assert client.update_calls[0][1] == "ailb-west-1"


# ---------------------------------------------------------------------------
# sync_ollama_deployment
# ---------------------------------------------------------------------------


class TestSyncOllamaDeployment:
    """Tests for the single-deployment sync path."""

    def test_deployment_not_found(self) -> None:
        """An unknown deployment id fails without touching AILB or routes."""
        service = ProviderSyncService(_FakeDB())
        result = service.sync_ollama_deployment(999)
        assert result.success is False
        assert result.status == SyncStatus.FAILED
        assert result.error == "Deployment not found"

    def test_no_models_assigned(self) -> None:
        """A deployment with zero models fails with a specific error."""
        db = _FakeDB()
        _seed_deployment(db, deployment_id=1)
        service = ProviderSyncService(db)

        result = service.sync_ollama_deployment(1)
        assert result.success is False
        assert result.error == "No models assigned to deployment"

    def test_success_inserts_new_route_record(self) -> None:
        """First-time sync inserts a new ollama_model_routes row, calls AILB, and commits."""
        db = _FakeDB()
        _seed_deployment(db, deployment_id=1)
        _seed_model(db, model_id=10, deployment_id=1, model_name="llama3", model_tag="8b")
        client = _FakeAILBClient()
        service = ProviderSyncService(db, client)
        service.set_instance_id("inst-1")

        result = service.sync_ollama_deployment(1)

        assert result.success is True
        assert result.status == SyncStatus.SYNCED
        assert result.message == "Synced 1 model routes"
        assert len(client.update_calls) == 1
        routes, instance_id = client.update_calls[0]
        assert instance_id == "inst-1"
        assert routes[0].route_id == "ollama-1-llama3"

        routes_table = db.table("ollama_model_routes")
        assert len(routes_table) == 1
        assert routes_table[0].model_id == 10
        assert routes_table[0].sync_status == "synced"
        assert routes_table[0].ailb_route_id == "ollama-1-llama3"
        assert db.commit_calls == 1

    def test_success_updates_existing_route_record_in_place(self) -> None:
        """A pre-existing sync record is updated, not duplicated."""
        db = _FakeDB()
        _seed_deployment(db, deployment_id=1)
        _seed_model(db, model_id=10, deployment_id=1, model_name="llama3")
        db.seed(
            "ollama_model_routes",
            model_id=10,
            deployment_id=1,
            ailb_instance_id="",
            ailb_route_id="stale-route-id",
            sync_status="failed",
            last_synced=None,
            sync_error="previous failure",
        )
        service = ProviderSyncService(db, _FakeAILBClient())

        result = service.sync_ollama_deployment(1)

        assert result.success is True
        routes_table = db.table("ollama_model_routes")
        assert len(routes_table) == 1  # updated, not duplicated
        assert routes_table[0].ailb_route_id == "ollama-1-llama3"
        assert routes_table[0].sync_status == "synced"
        assert routes_table[0].sync_error is None

    def test_skips_ailb_call_when_client_not_connected(self) -> None:
        """A disconnected AILB client is not called, but DB records still sync."""
        db = _FakeDB()
        _seed_deployment(db, deployment_id=1)
        _seed_model(db, model_id=10, deployment_id=1)
        client = _FakeAILBClient(connected=False)
        service = ProviderSyncService(db, client)

        result = service.sync_ollama_deployment(1)

        assert result.success is True
        assert client.update_calls == []
        assert db.table("ollama_model_routes")[0].sync_status == "synced"

    def test_skips_ailb_call_when_no_client_configured(self) -> None:
        """No AILB client at all is treated the same as disconnected."""
        db = _FakeDB()
        _seed_deployment(db, deployment_id=1)
        _seed_model(db, model_id=10, deployment_id=1)
        service = ProviderSyncService(db, ailb_client=None)

        result = service.sync_ollama_deployment(1)
        assert result.success is True

    def test_ailb_reports_failure_message(self) -> None:
        """An AILB `{"success": False, "message": ...}` response fails the sync."""
        db = _FakeDB()
        _seed_deployment(db, deployment_id=1)
        _seed_model(db, model_id=10, deployment_id=1)
        client = _FakeAILBClient(update_result={"success": False, "message": "quota exceeded"})
        service = ProviderSyncService(db, client)

        result = service.sync_ollama_deployment(1)

        assert result.success is False
        assert result.status == SyncStatus.FAILED
        assert result.error == "quota exceeded"
        # Failure is caught before any local route record is written.
        assert db.table("ollama_model_routes") == []

    def test_ailb_raises_is_caught_and_reported(self) -> None:
        """An exception from the AILB client is caught, not propagated."""
        db = _FakeDB()
        _seed_deployment(db, deployment_id=1)
        _seed_model(db, model_id=10, deployment_id=1, model_name="badmodel")
        client = _FakeAILBClient(fail_route_prefix="ollama-1")
        service = ProviderSyncService(db, client)

        result = service.sync_ollama_deployment(1)

        assert result.success is False
        assert "AILB unreachable" in result.error


# ---------------------------------------------------------------------------
# _ollama_model_to_route
# ---------------------------------------------------------------------------


class TestOllamaModelToRoute:
    """Tests for the deployment+model -> RouteConfig conversion."""

    def test_defaults_port_when_url_has_none(self) -> None:
        """No explicit port in the endpoint URL falls back to 11434."""
        service = ProviderSyncService(_FakeDB())
        deployment = _Row(id=1, endpoint_url="http://ollama-host", name="dep")
        model = _Row(id=1, model_name="llama3", model_tag=None)

        route = service._ollama_model_to_route(deployment, model)

        assert route.destination_port == 11434
        assert route.protocol == "PROTOCOL_HTTP"
        assert route.metadata["model_tag"] == "latest"

    def test_uses_explicit_port_and_https(self) -> None:
        """An explicit port and https scheme are both reflected in the route."""
        service = ProviderSyncService(_FakeDB())
        deployment = _Row(id=2, endpoint_url="https://ollama-host:8443", name="dep2")
        model = _Row(id=5, model_name="mistral", model_tag="v1")

        route = service._ollama_model_to_route(deployment, model)

        assert route.destination_port == 8443
        assert route.protocol == "PROTOCOL_HTTPS"
        assert route.route_id == "ollama-2-mistral"
        assert route.headers["X-Ollama-Model"] == "mistral"
        assert route.priority == 200
        assert route.path_pattern == "/v1/chat/completions"
        assert route.metadata["model_tag"] == "v1"


# ---------------------------------------------------------------------------
# sync_all_ollama_deployments
# ---------------------------------------------------------------------------


class TestSyncAllOllamaDeployments:
    """Tests for the fleet-wide sync loop and its error isolation."""

    def test_empty_fleet_returns_empty_dict(self) -> None:
        """No deployments at all -> an empty results dict, no error."""
        service = ProviderSyncService(_FakeDB())
        assert service.sync_all_ollama_deployments() == {}

    def test_skips_deployments_with_no_models(self) -> None:
        """A running deployment with zero models is not synced or reported."""
        db = _FakeDB()
        _seed_deployment(db, deployment_id=1, status="running")
        service = ProviderSyncService(db)

        results = service.sync_all_ollama_deployments()
        assert results == {}

    def test_skips_deployments_outside_running_or_pending(self) -> None:
        """A stopped deployment is excluded even if it has models."""
        db = _FakeDB()
        _seed_deployment(db, deployment_id=1, status="stopped")
        _seed_model(db, model_id=1, deployment_id=1)
        service = ProviderSyncService(db)

        results = service.sync_all_ollama_deployments()
        assert results == {}

    def test_one_bad_deployment_does_not_abort_the_rest(self) -> None:
        """A deployment whose AILB call raises is isolated -- the others still sync."""
        db = _FakeDB()
        _seed_deployment(db, deployment_id=1, status="running")
        _seed_model(db, model_id=10, deployment_id=1, model_name="badmodel")
        _seed_deployment(db, deployment_id=2, status="pending")
        _seed_model(db, model_id=20, deployment_id=2, model_name="goodmodel")
        client = _FakeAILBClient(fail_route_prefix="ollama-1")
        service = ProviderSyncService(db, client)

        results = service.sync_all_ollama_deployments()

        assert set(results) == {1, 2}
        assert results[1].success is False
        assert results[2].success is True


# ---------------------------------------------------------------------------
# remove_ollama_model_route
# ---------------------------------------------------------------------------


class TestRemoveOllamaModelRoute:
    """Tests for removing a single model's AILB route."""

    def test_no_sync_record_is_a_noop_success(self) -> None:
        """Nothing to remove is treated as success, not an error."""
        service = ProviderSyncService(_FakeDB())
        assert service.remove_ollama_model_route(999) is True

    def test_removes_record_and_calls_ailb_when_route_id_present(self) -> None:
        """An existing route id triggers an AILB delete and removes the local record."""
        db = _FakeDB()
        db.seed("ollama_model_routes", model_id=10, ailb_route_id="ollama-1-llama3")
        client = _FakeAILBClient()
        service = ProviderSyncService(db, client)
        service.set_instance_id("inst-1")

        assert service.remove_ollama_model_route(10) is True
        assert client.delete_calls == [("ollama-1-llama3", "inst-1")]
        assert db.table("ollama_model_routes") == []
        assert db.commit_calls == 1

    def test_skips_ailb_call_when_route_id_missing(self) -> None:
        """A sync record with no ailb_route_id skips the AILB call but still removes locally."""
        db = _FakeDB()
        db.seed("ollama_model_routes", model_id=10, ailb_route_id=None)
        client = _FakeAILBClient()
        service = ProviderSyncService(db, client)

        assert service.remove_ollama_model_route(10) is True
        assert client.delete_calls == []
        assert db.table("ollama_model_routes") == []

    def test_skips_ailb_call_when_no_client_configured(self) -> None:
        """No AILB client configured skips the AILB branch entirely."""
        db = _FakeDB()
        db.seed("ollama_model_routes", model_id=10, ailb_route_id="ollama-1-llama3")
        service = ProviderSyncService(db, ailb_client=None)

        assert service.remove_ollama_model_route(10) is True
        assert db.table("ollama_model_routes") == []

    def test_exception_is_caught_and_returns_false(self) -> None:
        """A failure deep in the removal path is caught, returning False, not raising."""
        db = _FakeDB()
        db.seed("ollama_model_routes", model_id=10, ailb_route_id="ollama-1-llama3")
        client = _FakeAILBClient(raise_on_delete=RuntimeError("ailb down"))
        service = ProviderSyncService(db, client)

        assert service.remove_ollama_model_route(10) is False
        # The local record is untouched since the AILB call raised first.
        assert len(db.table("ollama_model_routes")) == 1


# ---------------------------------------------------------------------------
# get_model_route_status
# ---------------------------------------------------------------------------


class TestGetModelRouteStatus:
    """Tests for the per-model sync status lookup."""

    def test_no_sync_record(self) -> None:
        """No sync record at all is reported distinctly from a synced/failed state."""
        service = ProviderSyncService(_FakeDB())
        status = service.get_model_route_status(999)
        assert status == {
            "synced": False,
            "status": "not_synced",
            "message": "No sync record found",
        }

    def test_sync_record_but_model_deleted(self) -> None:
        """A sync record whose model no longer exists is reported as model_not_found."""
        db = _FakeDB()
        db.seed("ollama_model_routes", model_id=10, ailb_route_id="r1")
        service = ProviderSyncService(db)

        status = service.get_model_route_status(10)
        assert status["synced"] is False
        assert status["status"] == "model_not_found"

    def test_full_status_with_last_synced(self) -> None:
        """A synced record with a real model returns the full status payload."""
        db = _FakeDB()
        _seed_model(db, model_id=10, deployment_id=1, model_name="llama3", model_tag="8b")
        last_synced = datetime(2025, 3, 1, 9, 30, 0)
        db.seed(
            "ollama_model_routes",
            model_id=10,
            deployment_id=1,
            ailb_route_id="ollama-1-llama3",
            sync_status="synced",
            last_synced=last_synced,
            sync_error=None,
        )
        service = ProviderSyncService(db)

        status = service.get_model_route_status(10)
        assert status["synced"] is True
        assert status["route_id"] == "ollama-1-llama3"
        assert status["deployment_id"] == 1
        assert status["last_synced"] == "2025-03-01T09:30:00"
        assert status["model_name"] == "llama3"
        assert status["model_tag"] == "8b"

    def test_last_synced_none_renders_as_none(self) -> None:
        """A NULL last_synced is rendered as None, not an AttributeError."""
        db = _FakeDB()
        _seed_model(db, model_id=11, deployment_id=1, model_name="phi3")
        db.seed(
            "ollama_model_routes",
            model_id=11,
            deployment_id=1,
            ailb_route_id="ollama-1-phi3",
            sync_status="pending",
            last_synced=None,
            sync_error="not yet synced",
        )
        service = ProviderSyncService(db)

        status = service.get_model_route_status(11)
        assert status["last_synced"] is None
        assert status["synced"] is False  # sync_status != "synced"
        assert status["sync_error"] == "not yet synced"
