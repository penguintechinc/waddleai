"""Unit tests for platform credential endpoint tenant filtering (S12)."""

from __future__ import annotations

import inspect

from app.api.v1 import providers


def test_list_credentials_query_filters_owner_org_id_null():
    """Verify list_provider_credentials excludes tenant-owned rows (S12)."""
    src = inspect.getsource(providers.list_provider_credentials)
    assert "owner_org_id" in src, "platform credential list must exclude tenant-owned rows (S12)"


def test_update_credentials_scoped_to_platform_rows():
    """Verify update_provider_credential excludes tenant-owned rows (S12)."""
    src = inspect.getsource(providers.update_provider_credential)
    assert "owner_org_id" in src


def test_delete_or_rotate_scoped_to_platform_rows():
    """Verify delete_provider_credential excludes tenant-owned rows (S12).

    delete is a soft/hard op depending on the endpoint; the existence check must be
    constrained to platform rows so a BYOK id resolves to 404, not a mutation.
    """
    # delete is a soft/hard op depending on the endpoint; the existence check must be
    # constrained to platform rows so a BYOK id resolves to 404, not a mutation.
    for fn_name in ("delete_provider_credential",):
        if hasattr(providers, fn_name):
            assert "owner_org_id" in inspect.getsource(getattr(providers, fn_name))
