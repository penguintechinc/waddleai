"""Unit tests for /api/v1/cache-configs CRUD (spec §6.4)."""

from datetime import datetime
from unittest.mock import MagicMock, patch

from tests.unit.management.conftest import make_mock_key, make_select_result


def make_mock_cache_config(
    config_id: int = 1,
    scope_type: str = "global",
    scope_ref=None,
    exact_enabled: bool = True,
    semantic_enabled: bool = False,
    semantic_threshold: float = 0.95,
    ttl_seconds: int = 86400,
    max_entry_kb: int = 256,
    anthropic_cache_control: bool = True,
) -> MagicMock:
    """Make mock cache config."""
    row = MagicMock()
    row.id = config_id
    row.scope_type = scope_type
    row.scope_ref = scope_ref
    row.exact_enabled = exact_enabled
    row.semantic_enabled = semantic_enabled
    row.semantic_threshold = semantic_threshold
    row.ttl_seconds = ttl_seconds
    row.max_entry_kb = max_entry_kb
    row.anthropic_cache_control = anthropic_cache_control
    row.created_at = datetime(2026, 1, 1, 0, 0, 0)
    row.updated_at = datetime(2026, 1, 1, 0, 0, 0)
    return row


class TestListAndGet:
    """Tests for list and get."""

    async def test_list_returns_rows(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """List returns rows."""
        rows = [make_mock_cache_config(1, "global"), make_mock_cache_config(2, "org", "1")]
        app_mock_db.return_value.select.side_effect = [make_select_result(rows)]

        resp = await client.get("/api/v1/cache-configs", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert len(data["data"]) == 2

    async def test_get_missing_returns_404(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Get missing returns 404."""
        app_mock_db.return_value.select.side_effect = [make_select_result([])]
        resp = await client.get("/api/v1/cache-configs/999", headers=auth_headers)
        assert resp.status_code == 404


class TestCreateValidation:
    """Tests for create validation."""

    async def test_missing_body_400(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Missing body 400."""
        resp = await client.post("/api/v1/cache-configs", headers=auth_headers, json=None)
        assert resp.status_code == 400

    async def test_invalid_scope_type_400(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Invalid scope type 400."""
        resp = await client.post(
            "/api/v1/cache-configs", headers=auth_headers, json={"scope_type": "bogus"}
        )
        assert resp.status_code == 400

    async def test_threshold_out_of_range_400(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Threshold out of range 400."""
        resp = await client.post(
            "/api/v1/cache-configs",
            headers=auth_headers,
            json={"scope_type": "org", "scope_ref": "1", "semantic_threshold": 0.2},
        )
        assert resp.status_code == 400

    async def test_ttl_not_positive_400(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Ttl not positive 400."""
        resp = await client.post(
            "/api/v1/cache-configs",
            headers=auth_headers,
            json={"scope_type": "org", "scope_ref": "1", "ttl_seconds": 0},
        )
        assert resp.status_code == 400

    async def test_org_scope_requires_scope_ref_400(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Org scope requires scope ref 400."""
        resp = await client.post(
            "/api/v1/cache-configs", headers=auth_headers, json={"scope_type": "org"}
        )
        assert resp.status_code == 400


class TestCreateHappyPath:
    """Tests for create happy path."""

    async def test_admin_creates_global_config(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin creates global config."""
        created_row = make_mock_cache_config(5, "global")
        # 1st select().first(): uniqueness check -> None. 2nd: post-insert fetch.
        app_mock_db.return_value.select.side_effect = [
            make_select_result([]),
            make_select_result([created_row]),
        ]
        app_mock_db.cache_configs.insert.return_value = 5

        with patch("services.management.app.api.v1.cache_configs.redis_client", MagicMock()):
            resp = await client.post(
                "/api/v1/cache-configs",
                headers=auth_headers,
                json={"scope_type": "global", "semantic_threshold": 0.97},
            )

        assert resp.status_code == 201
        data = await resp.get_json()
        assert data["data"]["scope_type"] == "global"

    async def test_create_conflict_returns_409(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Create conflict returns 409."""
        existing_row = make_mock_cache_config(1, "org", "1")
        app_mock_db.return_value.select.side_effect = [make_select_result([existing_row])]

        resp = await client.post(
            "/api/v1/cache-configs",
            headers=auth_headers,
            json={"scope_type": "org", "scope_ref": "1"},
        )
        assert resp.status_code == 409

    async def test_resource_manager_cannot_write_another_orgs_row(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        # rm_auth_headers user is org_id=1 (see conftest.make_token default org_id=1)
        """Resource manager cannot write another orgs row."""
        resp = await client.post(
            "/api/v1/cache-configs",
            headers=rm_auth_headers,
            json={"scope_type": "org", "scope_ref": "999"},
        )
        assert resp.status_code == 403

    async def test_resource_manager_cannot_write_global(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """Resource manager cannot write global."""
        resp = await client.post(
            "/api/v1/cache-configs", headers=rm_auth_headers, json={"scope_type": "global"}
        )
        assert resp.status_code == 403

    async def test_resource_manager_can_write_own_org_row(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """Resource manager can write own org row."""
        created_row = make_mock_cache_config(7, "org", "1")
        app_mock_db.return_value.select.side_effect = [
            make_select_result([]),
            make_select_result([created_row]),
        ]
        app_mock_db.cache_configs.insert.return_value = 7

        with patch("services.management.app.api.v1.cache_configs.redis_client", MagicMock()):
            resp = await client.post(
                "/api/v1/cache-configs",
                headers=rm_auth_headers,
                json={"scope_type": "org", "scope_ref": "1"},
            )
        assert resp.status_code == 201


class TestUpdateAndDelete:
    """Tests for update and delete."""

    async def test_update_missing_config_404(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Update missing config 404."""
        app_mock_db.return_value.select.side_effect = [make_select_result([])]
        resp = await client.put(
            "/api/v1/cache-configs/999", headers=auth_headers, json={"ttl_seconds": 100}
        )
        assert resp.status_code == 404

    async def test_update_invalidates_scope_and_returns_row(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Update invalidates scope and returns row."""
        existing_row = make_mock_cache_config(1, "global", ttl_seconds=86400)
        updated_row = make_mock_cache_config(1, "global", ttl_seconds=100)
        app_mock_db.return_value.select.side_effect = [
            make_select_result([existing_row]),
            make_select_result([updated_row]),
        ]
        fake_redis = MagicMock()

        with patch("services.management.app.api.v1.cache_configs.redis_client", fake_redis):
            resp = await client.put(
                "/api/v1/cache-configs/1", headers=auth_headers, json={"ttl_seconds": 100}
            )

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"]["ttl_seconds"] == 100
        fake_redis.delete.assert_called_once()

    async def test_delete_returns_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Delete returns success."""
        existing_row = make_mock_cache_config(1, "global")
        app_mock_db.return_value.select.side_effect = [make_select_result([existing_row])]

        with patch("services.management.app.api.v1.cache_configs.redis_client", MagicMock()):
            resp = await client.delete("/api/v1/cache-configs/1", headers=auth_headers)

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"]["deleted"] is True


class TestReadTenantIsolation:
    """GET /api/v1/cache-configs{,/<id>} must only expose the caller's own scopes.

    regression: audit-2026-09-14 (MEDIUM, cache_configs.py:113 and :132) --
    both read routes carried only @require_auth and did no organization_id
    filtering at all, so any authenticated user enumerated every
    organization's cache configuration. The write handlers in the same file
    were already gated by `_authorize_scope_write`, making this an
    asymmetric read-path gap.
    """

    async def test_list_hides_other_orgs_rows(
        self, client, app_mock_db: MagicMock, rm_org2_auth_headers: dict
    ) -> None:
        """# regression: audit-2026-09-14 -- an org-2 caller never sees org 1's row."""
        rows = [
            make_mock_cache_config(1, "global"),
            make_mock_cache_config(2, "org", "1"),
            make_mock_cache_config(3, "org", "2"),
            make_mock_cache_config(4, "key", "77"),
        ]
        app_mock_db.return_value.select.return_value = make_select_result(rows)

        resp = await client.get("/api/v1/cache-configs", headers=rm_org2_auth_headers)

        assert resp.status_code == 200
        data = await resp.get_json()
        returned = {(r["scope_type"], r["scope_ref"]) for r in data["data"]}
        # Own org row + the global default only: never org 1's row, and never
        # a key-scoped row (whose owning org cannot be resolved here).
        assert returned == {("global", None), ("org", "2")}
        assert ("org", "1") not in returned

    async def test_list_still_returns_everything_to_admin(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """# regression: audit-2026-09-14 -- the fix must not over-restrict admin."""
        rows = [
            make_mock_cache_config(1, "global"),
            make_mock_cache_config(2, "org", "1"),
            make_mock_cache_config(3, "org", "2"),
            make_mock_cache_config(4, "key", "77"),
        ]
        app_mock_db.return_value.select.return_value = make_select_result(rows)

        resp = await client.get("/api/v1/cache-configs", headers=auth_headers)

        assert resp.status_code == 200
        data = await resp.get_json()
        assert len(data["data"]) == 4

    async def test_get_by_id_of_another_orgs_row_is_not_found(
        self, client, app_mock_db: MagicMock, rm_org2_auth_headers: dict
    ) -> None:
        """# regression: audit-2026-09-14 -- org 1's row is invisible to an org-2 caller."""
        row = make_mock_cache_config(2, "org", "1", ttl_seconds=999)
        app_mock_db.return_value.select.return_value = make_select_result([row])

        resp = await client.get("/api/v1/cache-configs/2", headers=rm_org2_auth_headers)

        # 404 rather than 403: the id must not be confirmed as existing.
        assert resp.status_code == 404
        body = await resp.get_data(as_text=True)
        assert "999" not in body

    async def test_get_by_id_of_key_scoped_row_is_not_found_for_non_admin(
        self, client, app_mock_db: MagicMock, rm_org2_auth_headers: dict
    ) -> None:
        """# regression: audit-2026-09-14 -- key-scoped rows are admin-only on read."""
        row = make_mock_cache_config(4, "key", "77", ttl_seconds=888)
        app_mock_db.return_value.select.return_value = make_select_result([row])

        resp = await client.get("/api/v1/cache-configs/4", headers=rm_org2_auth_headers)

        assert resp.status_code == 404
        body = await resp.get_data(as_text=True)
        assert "888" not in body

    async def test_get_by_id_of_own_org_row_still_succeeds(
        self, client, app_mock_db: MagicMock, rm_org2_auth_headers: dict
    ) -> None:
        """# regression: audit-2026-09-14 -- the caller's own org row stays readable."""
        row = make_mock_cache_config(3, "org", "2")
        app_mock_db.return_value.select.return_value = make_select_result([row])

        resp = await client.get("/api/v1/cache-configs/3", headers=rm_org2_auth_headers)

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"]["scope_ref"] == "2"

    async def test_get_by_id_of_global_row_still_succeeds(
        self, client, app_mock_db: MagicMock, rm_org2_auth_headers: dict
    ) -> None:
        """# regression: audit-2026-09-14 -- the global default stays readable by everyone."""
        row = make_mock_cache_config(1, "global")
        app_mock_db.return_value.select.return_value = make_select_result([row])

        resp = await client.get("/api/v1/cache-configs/1", headers=rm_org2_auth_headers)

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"]["scope_type"] == "global"


class TestWriteScopeAuthorizationIsExhaustive:
    """`_authorize_scope_write` must authorize every scope type, not just global/org.

    regression: audit-2026-09-14 -- the helper branched only on "global"
    and "org" and then returned None (allowed) for anything else. "key" is
    a valid scope type, so a caller submitting scope_type="key" with
    another organization's virtual-key id passed authorization untouched
    and could create, update or delete that tenant's response-cache
    behaviour. These tests are falsifiable because the key -> owning-org
    lookup goes through the mocked DB, so the row the test feeds it is
    exactly what the authorization decision is made on: delete the check
    and the refusals below turn into 201/200.
    """

    async def test_non_admin_cannot_create_key_config_for_another_orgs_key(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """# regression: audit-2026-09-14 -- key scope is tenant-checked like org scope."""
        # rm_auth_headers is org 1; this virtual key belongs to org 2.
        # conftest caches the _DBTable per table name for the module-scoped
        # app and its insert mock is not a child of mock_db, so app_mock_db's
        # reset_mock() leaves earlier tests' calls on it.
        app_mock_db.cache_configs.insert.reset_mock()
        app_mock_db.return_value.select.side_effect = [
            make_select_result([make_mock_key(key_id=77, org_id=2)])
        ]

        resp = await client.post(
            "/api/v1/cache-configs",
            headers=rm_auth_headers,
            json={"scope_type": "key", "scope_ref": "77"},
        )

        assert resp.status_code == 403
        app_mock_db.cache_configs.insert.assert_not_called()

    async def test_non_admin_can_create_key_config_for_own_orgs_key(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """# regression: audit-2026-09-14 -- the fix must not block a legitimate key write."""
        created_row = make_mock_cache_config(9, "key", "55")
        app_mock_db.return_value.select.side_effect = [
            make_select_result([make_mock_key(key_id=55, org_id=1)]),  # ownership lookup
            make_select_result([]),  # no existing row for this scope
            make_select_result([created_row]),  # the created row
        ]
        app_mock_db.cache_configs.insert.return_value = 9

        with patch("services.management.app.api.v1.cache_configs.redis_client", MagicMock()):
            resp = await client.post(
                "/api/v1/cache-configs",
                headers=rm_auth_headers,
                json={"scope_type": "key", "scope_ref": "55"},
            )

        assert resp.status_code == 201

    async def test_non_admin_cannot_create_key_config_for_unknown_key(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """# regression: audit-2026-09-14 -- an unresolvable key denies, never falls through.

        Uses `return_value` rather than a `side_effect` sequence so the
        number of select() calls does not change the outcome: every select
        returns "nothing found", whichever code path runs. That makes the
        pre-fix and post-fix behaviours cleanly comparable -- with the
        authorization deleted this returns 201 and writes the row, with it
        in place it returns 403 and writes nothing.
        """
        app_mock_db.cache_configs.insert.reset_mock()  # see sibling test above
        app_mock_db.return_value.select.side_effect = None
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.post(
            "/api/v1/cache-configs",
            headers=rm_auth_headers,
            json={"scope_type": "key", "scope_ref": "12345"},
        )

        # Assert the write first: it is the actual security property, and it
        # is what fails with the authorization removed (the pre-fix code
        # inserts the row, then 500s serializing the mock's empty re-read --
        # a 403-vs-500 status diff would be a far muddier signal).
        app_mock_db.cache_configs.insert.assert_not_called()
        assert resp.status_code == 403

    async def test_admin_may_still_write_any_key_scope(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """# regression: audit-2026-09-14 -- admin keeps cross-org key writes."""
        created_row = make_mock_cache_config(10, "key", "77")
        app_mock_db.return_value.select.side_effect = [
            make_select_result([]),  # no existing row (admin needs no lookup)
            make_select_result([created_row]),
        ]
        app_mock_db.cache_configs.insert.return_value = 10

        with patch("services.management.app.api.v1.cache_configs.redis_client", MagicMock()):
            resp = await client.post(
                "/api/v1/cache-configs",
                headers=auth_headers,
                json={"scope_type": "key", "scope_ref": "77"},
            )

        assert resp.status_code == 201

    async def test_non_admin_cannot_delete_another_orgs_key_config(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """# regression: audit-2026-09-14 -- DELETE authorizes through the same helper."""
        app_mock_db.return_value.select.side_effect = [
            make_select_result([make_mock_cache_config(4, "key", "77")]),  # existing row
            make_select_result([make_mock_key(key_id=77, org_id=2)]),  # owned by org 2
        ]

        resp = await client.delete("/api/v1/cache-configs/4", headers=rm_auth_headers)

        assert resp.status_code == 403
        app_mock_db.return_value.delete.assert_not_called()

    async def test_non_admin_cannot_update_another_orgs_key_config(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """# regression: audit-2026-09-14 -- PUT authorizes through the same helper."""
        app_mock_db.return_value.select.side_effect = [
            make_select_result([make_mock_cache_config(4, "key", "77")]),
            make_select_result([make_mock_key(key_id=77, org_id=2)]),
        ]

        resp = await client.put(
            "/api/v1/cache-configs/4", headers=rm_auth_headers, json={"ttl_seconds": 60}
        )

        assert resp.status_code == 403
        app_mock_db.return_value.update.assert_not_called()

    async def test_unrecognized_scope_type_is_denied_not_allowed(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """# regression: audit-2026-09-14 -- the fall-through default must deny.

        Reached via DELETE, where scope_type comes off the stored row rather
        than a validated payload: a scope type this helper does not know
        about (a future one, or a corrupted row) must be refused, which is
        precisely the defaulting bug that let key-scoped writes through.
        """
        app_mock_db.return_value.select.side_effect = [
            make_select_result([make_mock_cache_config(5, "team", "99")])
        ]

        resp = await client.delete("/api/v1/cache-configs/5", headers=rm_auth_headers)

        assert resp.status_code == 403
        app_mock_db.return_value.delete.assert_not_called()


class TestPaginationAndResponseSchema:
    """Wave-2 audit: the list select is bounded, and every route emits a fixed field set.

    regression: audit-2026-09-14-wave2 -- ``GET /cache-configs`` ran an
    unbounded ``select()`` (the DoS/resource finding), and the CRUD routes
    carried no ``@validate_response`` schema. The mocked DB ignores
    ``limitby`` (it returns whatever rows the test feeds), so these assert on
    the response envelope and the ``select`` call args -- the observable the
    #239 stream documented -- not on row counts the mock cannot enforce.
    """

    async def test_list_carries_pagination_meta(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """# regression: audit-2026-09-14-wave2 -- list responses carry a pagination block."""
        app_mock_db.return_value.select.side_effect = [
            make_select_result([make_mock_cache_config(1, "global")])
        ]
        resp = await client.get("/api/v1/cache-configs?limit=5&page=2", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert set(data.keys()) == {"status", "data", "pagination"}
        assert data["pagination"]["limit"] == 5
        assert data["pagination"]["page"] == 2

    async def test_list_select_is_bounded_by_limitby(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """# regression: audit-2026-09-14-wave2 -- the bound is applied at query level."""
        app_mock_db.return_value.select.side_effect = [
            make_select_result([make_mock_cache_config(1, "global")])
        ]
        await client.get("/api/v1/cache-configs", headers=auth_headers)
        assert "limitby" in app_mock_db.return_value.select.call_args.kwargs

    async def test_list_limit_is_clamped_to_ceiling(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """# regression: audit-2026-09-14-wave2 -- a hostile ?limit is clamped, not honoured."""
        app_mock_db.return_value.select.side_effect = [make_select_result([])]
        resp = await client.get("/api/v1/cache-configs?limit=99999999", headers=auth_headers)
        data = await resp.get_json()
        assert data["pagination"]["limit"] == 1000  # _pagination.MAX_PAGE_SIZE

    async def test_get_single_field_set(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """# regression: audit-2026-09-14-wave2 -- get-single envelope is status+data only."""
        app_mock_db.return_value.select.side_effect = [
            make_select_result([make_mock_cache_config(1, "global")])
        ]
        resp = await client.get("/api/v1/cache-configs/1", headers=auth_headers)
        data = await resp.get_json()
        assert set(data.keys()) == {"status", "data"}

    async def test_delete_field_set(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """# regression: audit-2026-09-14-wave2 -- delete envelope is status + {id, deleted}."""
        app_mock_db.return_value.select.side_effect = [
            make_select_result([make_mock_cache_config(1, "global")])
        ]
        with patch("services.management.app.api.v1.cache_configs.redis_client", MagicMock()):
            resp = await client.delete("/api/v1/cache-configs/1", headers=auth_headers)
        data = await resp.get_json()
        assert set(data.keys()) == {"status", "data"}
        assert set(data["data"].keys()) == {"id", "deleted"}
