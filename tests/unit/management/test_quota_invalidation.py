"""Quota config invalidation is best-effort: it deletes the right keys and never raises."""

from unittest.mock import MagicMock

from services.management.app import extensions
from services.management.app.services.quota_invalidation import invalidate_quota_config
from shared.quota import keys


async def test_deletes_exactly_the_targeted_blobs(monkeypatch):
    """Org + team ids map to their two cache keys in one DELETE."""
    client = MagicMock()
    monkeypatch.setattr(extensions, "redis_client", client)
    assert await invalidate_quota_config(org_id=2, team_id=5) == 2
    client.delete.assert_called_once_with(keys.org_config(2), keys.team_config(5))


async def test_org_only_and_team_only_map_to_their_own_key(monkeypatch):
    """Each scope deletes only its own key (the negative 'null' entry shares that key)."""
    client = MagicMock()
    monkeypatch.setattr(extensions, "redis_client", client)
    assert await invalidate_quota_config(org_id=7) == 1
    client.delete.assert_called_with(keys.org_config(7))
    assert await invalidate_quota_config(team_id=8) == 1
    client.delete.assert_called_with(keys.team_config(8))


async def test_nothing_to_do_touches_nothing(monkeypatch):
    """No ids -> 0 targeted and the client is never used."""
    client = MagicMock()
    monkeypatch.setattr(extensions, "redis_client", client)
    assert await invalidate_quota_config() == 0
    client.delete.assert_not_called()


async def test_missing_client_is_logged_not_raised(monkeypatch, caplog):
    """No cache client: report the count, warn, rely on the proxy TTL."""
    monkeypatch.setattr(extensions, "redis_client", None)
    with caplog.at_level("WARNING"):
        assert await invalidate_quota_config(org_id=1) == 1
    assert "skipped" in caplog.text


async def test_valkey_error_is_swallowed(monkeypatch, caplog):
    """A Valkey failure must never fail the config write that triggered it."""
    client = MagicMock()
    client.delete.side_effect = ConnectionError("down")
    monkeypatch.setattr(extensions, "redis_client", client)
    with caplog.at_level("WARNING"):
        assert await invalidate_quota_config(team_id=9) == 1
    assert "failed to invalidate" in caplog.text
