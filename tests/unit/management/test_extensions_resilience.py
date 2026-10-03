"""O5/O4 (ops audit): DB init backoff+jitter and the Valkey/Redis connection pool bound.

init_db previously retried on a fixed 2s `time.sleep` x 10 with no jitter; a
sustained outage had every management replica retrying in lockstep. init_cache
previously called `redis.from_url()` with no max_connections/socket timeouts,
so a slow/hung Valkey could exhaust connections with no bound.
"""

from unittest.mock import MagicMock, patch

import pytest

from services.management.app import extensions as ext


class TestBackoffDelay:
    """_backoff_delay implements exponential backoff with full jitter."""

    def test_delay_is_bounded_by_cap(self) -> None:
        """Every delay, for any attempt, stays within [0, cap]."""
        for attempt in range(1, 12):
            delay = ext._backoff_delay(attempt, base=2.0, cap=30.0)
            assert 0.0 <= delay <= 30.0

    def test_delay_grows_with_attempt_before_the_cap(self) -> None:
        """The jitter ceiling for a later attempt exceeds an earlier one's, below the cap."""
        # With a high cap, the ceiling for later attempts must exceed earlier ones.
        with patch("random.uniform", side_effect=lambda lo, hi: hi):
            early = ext._backoff_delay(1, base=2.0, cap=1000.0)
            later = ext._backoff_delay(4, base=2.0, cap=1000.0)
        assert later > early

    def test_delay_saturates_at_cap_for_large_attempts(self) -> None:
        """A large attempt number saturates at the cap instead of growing unbounded."""
        with patch("random.uniform", side_effect=lambda lo, hi: hi):
            delay = ext._backoff_delay(20, base=2.0, cap=30.0)
        assert delay == 30.0


class TestInitDbRetry:
    """init_db retries with backoff and eventually succeeds or raises."""

    def _app(self, tmp_path) -> MagicMock:
        app = MagicMock()
        app.config = {"DATABASE_URL": f"sqlite:///{tmp_path}/t.db", "DB_POOL_SIZE": 5}
        return app

    def test_succeeds_without_sleeping_on_first_try(self, tmp_path, monkeypatch) -> None:
        """A first-attempt success never pays a retry backoff sleep."""
        monkeypatch.delenv("DB_MAX_RETRIES", raising=False)
        monkeypatch.delenv("DB_RETRY_DELAY", raising=False)
        fake_db = MagicMock()
        with (
            patch("app.models_sqlalchemy.init_schema"),
            patch.object(ext, "init_dal", return_value=fake_db),
            patch("time.sleep") as sleep_mock,
        ):
            with patch("services.management.app.extensions.DB", type(fake_db)):
                result = ext.init_db(self._app(tmp_path))
        assert result is fake_db
        # init_db always sleeps once for the pre-flight DNS wait (sleep(5)),
        # unrelated to retry backoff -- no *additional* backoff sleep happens
        # on a first-try success.
        sleep_mock.assert_called_once_with(5)

    def test_retries_with_increasing_backoff_then_succeeds(self, tmp_path, monkeypatch) -> None:
        """Each retry's backoff delay strictly increases until the connection succeeds."""
        monkeypatch.setenv("DB_MAX_RETRIES", "4")
        monkeypatch.setenv("DB_RETRY_DELAY", "1")
        monkeypatch.setenv("DB_RETRY_MAX_DELAY", "30")
        fake_db = MagicMock()
        attempts = {"n": 0}

        def _flaky_init_schema(_url):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("db not ready yet")

        with (
            patch("app.models_sqlalchemy.init_schema", side_effect=_flaky_init_schema),
            patch.object(ext, "init_dal", return_value=fake_db),
            patch("time.sleep") as sleep_mock,
            patch("random.uniform", side_effect=lambda lo, hi: hi),
        ):
            with patch("services.management.app.extensions.DB", type(fake_db)):
                result = ext.init_db(self._app(tmp_path))

        assert result is fake_db
        assert attempts["n"] == 3
        # Two failed attempts before success -> two backoff sleeps, strictly increasing
        # (base=1, cap=30: attempt1 -> min(30, 2)=2, attempt2 -> min(30, 4)=4).
        # First call is the unrelated pre-flight DNS sleep(5); the two backoff
        # sleeps that follow strictly increase (base=1, cap=30: attempt1 ->
        # min(30, 2)=2, attempt2 -> min(30, 4)=4).
        delays = [call.args[0] for call in sleep_mock.call_args_list]
        assert delays == [5, 2.0, 4.0]

    def test_exhausting_retries_raises(self, tmp_path, monkeypatch) -> None:
        """A permanently failing DB re-raises once DB_MAX_RETRIES is exhausted."""
        monkeypatch.setenv("DB_MAX_RETRIES", "3")
        monkeypatch.setenv("DB_RETRY_DELAY", "1")
        with (
            patch(
                "app.models_sqlalchemy.init_schema",
                side_effect=RuntimeError("db permanently down"),
            ),
            patch("time.sleep"),
        ):
            with pytest.raises(RuntimeError, match="db permanently down"):
                ext.init_db(self._app(tmp_path))

    def test_falls_back_to_app_extensions_when_init_dal_returns_none(
        self, tmp_path, monkeypatch
    ) -> None:
        """penguin-dal 0.1.0's init_dal parks the DB on app.extensions instead of returning it."""
        monkeypatch.delenv("DB_MAX_RETRIES", raising=False)
        fake_db = MagicMock()
        app = self._app(tmp_path)
        app.extensions = {"_penguin_dal": fake_db}
        with (
            patch("app.models_sqlalchemy.init_schema"),
            patch.object(ext, "init_dal", return_value=None),
            patch("time.sleep"),
            patch("services.management.app.extensions.DB", type(fake_db)),
        ):
            result = ext.init_db(app)
        assert result is fake_db

    def test_raises_type_error_when_init_dal_returns_wrong_type(
        self, tmp_path, monkeypatch
    ) -> None:
        """A DatabaseManager (read/write split) slipping through must fail loudly, not silently."""
        monkeypatch.delenv("DB_MAX_RETRIES", raising=False)

        class _NotADB:
            pass

        with (
            patch("app.models_sqlalchemy.init_schema"),
            patch.object(ext, "init_dal", return_value=_NotADB()),
            patch("time.sleep"),
        ):
            with pytest.raises(TypeError, match="expected DB"):
                ext.init_db(self._app(tmp_path))


class TestInitCacheConnectionPool:
    """init_cache bounds the Valkey/Redis connection pool and socket timeouts."""

    def _app_with_cache_host(self) -> MagicMock:
        app = MagicMock()
        app.config = {
            "CACHE_HOST": "valkey.internal",
            "CACHE_PORT": 6379,
            "CACHE_USER": "",
            "CACHE_PASS": "",
        }
        return app

    def test_passes_max_connections_and_timeouts(self, monkeypatch) -> None:
        """The default pool bound and socket timeouts are passed to redis.from_url."""
        monkeypatch.delenv("MANAGEMENT_VALKEY_MAX_CONNECTIONS", raising=False)
        fake_client = MagicMock()
        with patch("redis.from_url", return_value=fake_client) as from_url:
            ext.init_cache(self._app_with_cache_host())

        _, kwargs = from_url.call_args
        assert kwargs["max_connections"] == 50
        assert kwargs["socket_timeout"] == 5.0
        assert kwargs["socket_connect_timeout"] == 5.0
        assert kwargs["health_check_interval"] == 30

    def test_max_connections_env_override(self, monkeypatch) -> None:
        """MANAGEMENT_VALKEY_MAX_CONNECTIONS overrides the default pool bound."""
        monkeypatch.setenv("MANAGEMENT_VALKEY_MAX_CONNECTIONS", "200")
        fake_client = MagicMock()
        with patch("redis.from_url", return_value=fake_client) as from_url:
            ext.init_cache(self._app_with_cache_host())

        assert from_url.call_args.kwargs["max_connections"] == 200

    def test_cache_user_without_password_builds_url_without_colon(self) -> None:
        """A CACHE_USER with no CACHE_PASS builds a user@host URL, no colon."""
        app = MagicMock()
        app.config = {
            "CACHE_HOST": "valkey.internal",
            "CACHE_PORT": 6379,
            "CACHE_USER": "svc",
            "CACHE_PASS": "",
        }
        with patch("redis.from_url", return_value=MagicMock()) as from_url:
            ext.init_cache(app)
        assert from_url.call_args.args[0] == "redis://svc@valkey.internal:6379/0"

    def test_falls_back_to_deprecated_redis_url(self) -> None:
        """With no CACHE_HOST, the deprecated REDIS_URL is used as-is."""
        app = MagicMock()
        app.config = {"CACHE_HOST": "", "REDIS_URL": "redis://legacy:6379/0"}
        with patch("redis.from_url", return_value=MagicMock()) as from_url:
            ext.init_cache(app)
        assert from_url.call_args.args[0] == "redis://legacy:6379/0"

    def test_no_cache_configured_returns_none(self) -> None:
        """With neither CACHE_HOST nor REDIS_URL set, init_cache returns None."""
        app = MagicMock()
        app.config = {"CACHE_HOST": "", "REDIS_URL": ""}
        assert ext.init_cache(app) is None

    def test_connection_failure_returns_none(self) -> None:
        """A connection error from redis.from_url is swallowed, returning None."""
        app = MagicMock()
        app.config = {
            "CACHE_HOST": "valkey.internal",
            "CACHE_PORT": 6379,
            "CACHE_USER": "",
            "CACHE_PASS": "",
        }
        with patch("redis.from_url", side_effect=ConnectionError("down")):
            assert ext.init_cache(app) is None
