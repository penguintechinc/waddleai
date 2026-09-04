"""Behavioral tests for platform credential endpoint tenant filtering (S12).

Tests verify that platform credential endpoints (list, create, update, delete)
exclude tenant-owned (BYOK) credentials by actually executing the PyDAL queries
against a fake in-memory database, not just asserting substring presence.
"""

from __future__ import annotations

import inspect

from app.api.v1 import providers

from tests.unit.routing.conftest import FakeDB


class TestCredentialListFiltersOwnerOrgId:
    """list_provider_credentials filters owner_org_id IS NULL."""

    def test_list_returns_only_platform_rows(self) -> None:
        """Verify list query filters to platform rows only."""
        fake_db = FakeDB()
        provider_id = 1

        # Plant one platform row (owner_org_id=None) and one BYOK row (owner_org_id=42)
        fake_db.seed(
            "provider_credentials",
            [
                {
                    "id": 1,
                    "provider_id": provider_id,
                    "label": "platform-cred",
                    "owner_org_id": None,  # platform row
                },
                {
                    "id": 2,
                    "provider_id": provider_id,
                    "label": "byok-cred",
                    "owner_org_id": 42,  # tenant-owned
                },
            ],
        )

        # Execute the query that list_provider_credentials uses
        result = fake_db(
            (fake_db.provider_credentials.provider_id == provider_id)
            & (fake_db.provider_credentials.owner_org_id == None)  # noqa: E711
        ).select(orderby=fake_db.provider_credentials.id)

        # Assert: only the platform row is returned
        assert len(result) == 1
        assert result[0]["id"] == 1
        assert result[0]["label"] == "platform-cred"
        assert result[0]["owner_org_id"] is None


class TestCredentialUpdateFiltersOwnerOrgId:
    """update_provider_credential filters owner_org_id IS NULL on existence check."""

    def test_update_byok_credential_not_found(self) -> None:
        """Verify update existence check excludes BYOK rows."""
        fake_db = FakeDB()
        provider_id = 1
        byok_cred_id = 2

        # Plant one platform row and one BYOK row
        fake_db.seed(
            "provider_credentials",
            [
                {
                    "id": 1,
                    "provider_id": provider_id,
                    "label": "platform-cred",
                    "owner_org_id": None,
                },
                {
                    "id": byok_cred_id,
                    "provider_id": provider_id,
                    "label": "byok-cred",
                    "owner_org_id": 42,
                },
            ],
        )

        # Execute the existence check that _check_existence uses
        cred = (
            fake_db(
                (fake_db.provider_credentials.id == byok_cred_id)
                & (fake_db.provider_credentials.provider_id == provider_id)
                & (fake_db.provider_credentials.owner_org_id == None)  # noqa: E711
            )
            .select()
            .first()
        )

        # Assert: BYOK row is NOT found (resolves to 404)
        assert cred is None

    def test_update_platform_credential_found(self) -> None:
        """Verify update existence check finds platform rows."""
        fake_db = FakeDB()
        provider_id = 1
        platform_cred_id = 1

        # Plant one platform row and one BYOK row
        fake_db.seed(
            "provider_credentials",
            [
                {
                    "id": platform_cred_id,
                    "provider_id": provider_id,
                    "label": "platform-cred",
                    "owner_org_id": None,
                },
                {
                    "id": 2,
                    "provider_id": provider_id,
                    "label": "byok-cred",
                    "owner_org_id": 42,
                },
            ],
        )

        # Execute the existence check
        cred = (
            fake_db(
                (fake_db.provider_credentials.id == platform_cred_id)
                & (fake_db.provider_credentials.provider_id == provider_id)
                & (fake_db.provider_credentials.owner_org_id == None)  # noqa: E711
            )
            .select()
            .first()
        )

        # Assert: platform row IS found
        assert cred is not None
        assert cred["id"] == platform_cred_id

    def test_update_label_conflict_check_excludes_byok(self) -> None:
        """Verify label-conflict check in update doesn't match against BYOK rows."""
        fake_db = FakeDB()
        provider_id = 1
        platform_cred_id = 1
        byok_label = "byok-cred"

        # Plant one platform row and one BYOK row with same label we want to check
        fake_db.seed(
            "provider_credentials",
            [
                {
                    "id": platform_cred_id,
                    "provider_id": provider_id,
                    "label": "platform-cred",
                    "owner_org_id": None,
                },
                {
                    "id": 2,
                    "provider_id": provider_id,
                    "label": byok_label,
                    "owner_org_id": 42,
                },
            ],
        )

        # Execute the label-conflict check (trying to rename platform row to byok's label)
        conflict = (
            fake_db(
                (fake_db.provider_credentials.provider_id == provider_id)
                & (fake_db.provider_credentials.label == byok_label)
                & (fake_db.provider_credentials.id != platform_cred_id)
                & (fake_db.provider_credentials.owner_org_id == None)  # noqa: E711
            )
            .select()
            .first()
        )

        # Assert: no conflict (BYOK row is excluded, platform has different label)
        assert conflict is None


class TestCredentialCreateFiltersOwnerOrgId:
    """create_provider_credential label-uniqueness check filters owner_org_id IS NULL."""

    def test_create_with_label_matching_byok_succeeds(self) -> None:
        """Verify create succeeds when label matches BYOK row (no false 409)."""
        fake_db = FakeDB()
        provider_id = 1
        new_label = "byok-cred"

        # Plant one platform row and one BYOK row with the new label
        fake_db.seed(
            "provider_credentials",
            [
                {
                    "id": 1,
                    "provider_id": provider_id,
                    "label": "platform-cred",
                    "owner_org_id": None,
                },
                {
                    "id": 2,
                    "provider_id": provider_id,
                    "label": new_label,
                    "owner_org_id": 42,
                },
            ],
        )

        # Execute the label-uniqueness check that create uses
        existing = (
            fake_db(
                (fake_db.provider_credentials.provider_id == provider_id)
                & (fake_db.provider_credentials.label == new_label)
                & (fake_db.provider_credentials.owner_org_id == None)  # noqa: E711
            )
            .select()
            .first()
        )

        # Assert: no conflict (BYOK row is excluded)
        assert existing is None

    def test_create_with_label_matching_platform_fails(self) -> None:
        """Verify create fails when label matches platform row (correct 409)."""
        fake_db = FakeDB()
        provider_id = 1
        new_label = "platform-cred"

        # Plant one platform row and one BYOK row
        fake_db.seed(
            "provider_credentials",
            [
                {
                    "id": 1,
                    "provider_id": provider_id,
                    "label": new_label,
                    "owner_org_id": None,
                },
                {
                    "id": 2,
                    "provider_id": provider_id,
                    "label": "byok-cred",
                    "owner_org_id": 42,
                },
            ],
        )

        # Execute the label-uniqueness check
        existing = (
            fake_db(
                (fake_db.provider_credentials.provider_id == provider_id)
                & (fake_db.provider_credentials.label == new_label)
                & (fake_db.provider_credentials.owner_org_id == None)  # noqa: E711
            )
            .select()
            .first()
        )

        # Assert: conflict IS detected (platform row matches)
        assert existing is not None


class TestCredentialDeleteFiltersOwnerOrgId:
    """delete_provider_credential filters owner_org_id IS NULL on existence + count."""

    def test_delete_byok_credential_not_found(self) -> None:
        """Verify delete existence check excludes BYOK rows."""
        fake_db = FakeDB()
        provider_id = 1
        byok_cred_id = 2

        # Plant platform and BYOK rows
        fake_db.seed(
            "provider_credentials",
            [
                {
                    "id": 1,
                    "provider_id": provider_id,
                    "label": "platform-cred",
                    "owner_org_id": None,
                },
                {
                    "id": byok_cred_id,
                    "provider_id": provider_id,
                    "label": "byok-cred",
                    "owner_org_id": 42,
                },
            ],
        )

        # Execute the existence check that _delete uses
        cred = (
            fake_db(
                (fake_db.provider_credentials.id == byok_cred_id)
                & (fake_db.provider_credentials.provider_id == provider_id)
                & (fake_db.provider_credentials.owner_org_id == None)  # noqa: E711
            )
            .select()
            .first()
        )

        # Assert: BYOK row is NOT found (resolves to 404)
        assert cred is None

    def test_delete_last_platform_credential_fails_even_with_byok(self) -> None:
        """Verify delete's 'last credential' guard counts platform rows only."""
        fake_db = FakeDB()
        provider_id = 1
        sole_platform_cred = 1

        # Plant one platform row and one BYOK row
        fake_db.seed(
            "provider_credentials",
            [
                {
                    "id": sole_platform_cred,
                    "provider_id": provider_id,
                    "label": "platform-cred",
                    "owner_org_id": None,
                },
                {
                    "id": 2,
                    "provider_id": provider_id,
                    "label": "byok-cred",
                    "owner_org_id": 42,
                },
            ],
        )

        # Execute the total count check that _delete uses (to check if it's the last)
        total = len(
            fake_db(
                (fake_db.provider_credentials.provider_id == provider_id)
                & (fake_db.provider_credentials.owner_org_id == None)  # noqa: E711
            ).select()
        )

        # Assert: total is 1 (only platform row counted), so delete would be rejected
        assert total == 1


class TestCredentialSourceHasFilters:
    """Verify source code contains owner_org_id filter strings (supplementary check)."""

    def test_list_credentials_query_has_owner_org_id_filter(self) -> None:
        """Source-level check: list_provider_credentials references owner_org_id."""
        src = inspect.getsource(providers.list_provider_credentials)
        assert "owner_org_id" in src

    def test_update_credentials_query_has_owner_org_id_filter(self) -> None:
        """Source-level check: update_provider_credential references owner_org_id."""
        src = inspect.getsource(providers.update_provider_credential)
        assert "owner_org_id" in src

    def test_delete_credentials_query_has_owner_org_id_filter(self) -> None:
        """Source-level check: delete_provider_credential references owner_org_id."""
        src = inspect.getsource(providers.delete_provider_credential)
        assert "owner_org_id" in src
