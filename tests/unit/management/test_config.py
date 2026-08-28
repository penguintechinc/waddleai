"""Unit tests for app.config's per-environment Config classes.

Covers env-var precedence for connection settings that vary by deployment
(DATABASE_URL, REDIS_URL) -- these must always honor an env override rather
than silently falling back to a hardcoded default, or a container started
with FLASK_ENV=testing against a non-localhost dependency (e.g. a
docker-compose harness where redis is a separate "redis" service) ends up
silently pointed at localhost instead.
"""

import importlib

import pytest


@pytest.fixture
def reload_config(monkeypatch):
    """Reload app.config after mutating env vars.

    Module-level consts are computed once at import time, so a plain
    monkeypatch.setenv is invisible to an already-imported module.
    """

    def _reload():
        import app.config as config_module

        return importlib.reload(config_module)

    yield _reload


class TestTestingConfigRedisUrl:
    """regression: gh-150.

    TestingConfig.REDIS_URL used to be a hardcoded "redis://localhost:6379/1"
    literal that ignored the REDIS_URL env var entirely (unlike DATABASE_URL
    in the same class, and unlike the base Config, both of which do honor
    it). In the release-branch CI integration-test harness
    (FLASK_ENV=testing, redis reachable only via the "redis" compose service
    name), this left init_cache() connecting to localhost:6379 -- nothing
    listens there -- so _ext.redis_client stayed None and /readyz 503'd
    forever, independent of database schema/migration state.
    """

    def test_honors_env_override(self, monkeypatch, reload_config):
        """REDIS_URL env var must override the hardcoded default."""
        monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
        config_module = reload_config()
        assert config_module.TestingConfig.REDIS_URL == "redis://redis:6379/0"

    def test_falls_back_to_localhost_default_when_unset(self, monkeypatch, reload_config):
        """With no REDIS_URL set, the original localhost default is preserved."""
        monkeypatch.delenv("REDIS_URL", raising=False)
        config_module = reload_config()
        assert config_module.TestingConfig.REDIS_URL == "redis://localhost:6379/1"

    def test_matches_database_url_precedence_pattern(self, monkeypatch, reload_config):
        """DATABASE_URL and REDIS_URL in TestingConfig behave symmetrically.

        Both are env-override-first, hardcoded-default-second.
        """
        monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@db:5432/waddleai_test")
        monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
        config_module = reload_config()
        assert (
            config_module.TestingConfig.DATABASE_URL
            == "postgresql://test:test@db:5432/waddleai_test"
        )
        assert config_module.TestingConfig.REDIS_URL == "redis://redis:6379/0"
