"""Coverage completion for `penguincode_cli/db/migrate.py`'s CLI entrypoint.

`tests/test_db_migrate.py` covers `run_migrations`/`_resolve_dsn`/
`_discover_migrations` thoroughly, but never calls `main()` or executes the
module as a script -- `main()` is the `python3 -m penguincode_cli.db.migrate`
entrypoint the Kubernetes init job actually invokes (see this module's own
docstring), so both matter.

Static tests (no DB needed) always run. The live-Postgres test connects to
`TEST_DATABASE_URL` and is skipped -- with an explicit reason, never
silently -- when that env var is unset, mirroring `tests/test_db_migrate.py`'s
own `requires_postgres` pattern.

# regression: penguincode db/migrate.py coverage gate (penguincode #cov)
"""

from __future__ import annotations

import os
import runpy
import sys

import pytest

import penguincode_cli.db.migrate as migrate_module
from penguincode_cli.db.migrate import MigrationResult

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set -- live-Postgres migrate CLI test is CI-pending",
)


class TestMainFunction:
    """`main()`'s body, with `run_migrations` faked -- no DB touched."""

    def test_explicit_dsn_argv_is_forwarded_and_prints_summary(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`argv[0]`, when given, becomes `run_migrations`'s `dsn=` -- and the
        applied/skipped summary is printed on success."""
        captured_dsn: dict[str, str | None] = {}

        def _fake_run_migrations(dsn: str | None = None) -> MigrationResult:
            captured_dsn["dsn"] = dsn
            return MigrationResult(applied=("0001_x.sql",), skipped=("0002_y.sql",))

        monkeypatch.setattr(migrate_module, "run_migrations", _fake_run_migrations)

        exit_code = migrate_module.main(["postgresql://explicit/db"])

        assert exit_code == 0
        assert captured_dsn["dsn"] == "postgresql://explicit/db"
        out = capsys.readouterr().out
        assert "applied=['0001_x.sql']" in out
        assert "skipped=['0002_y.sql']" in out

    def test_no_argv_falls_back_to_sys_argv_and_none_dsn(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`argv=None` falls back to `sys.argv[1:]`; an empty arg list means
        `dsn=None` is forwarded (letting `run_migrations` resolve `PGVECTOR_URL`)."""
        captured_dsn: dict[str, str | None] = {}

        def _fake_run_migrations(dsn: str | None = None) -> MigrationResult:
            captured_dsn["dsn"] = dsn
            return MigrationResult(applied=(), skipped=())

        monkeypatch.setattr(migrate_module, "run_migrations", _fake_run_migrations)
        monkeypatch.setattr(sys, "argv", ["migrate.py"])

        exit_code = migrate_module.main(None)

        assert exit_code == 0
        assert captured_dsn["dsn"] is None
        out = capsys.readouterr().out
        assert "applied=[] skipped=[]" in out


@requires_postgres
class TestMainGuardLive:
    """Executes the module as a script (`__name__ == "__main__"`) against a
    real Postgres -- the one behavior `main()`-as-a-function can't prove: the
    `if __name__ == "__main__": raise SystemExit(main())` guard itself runs
    and exits 0.
    """

    def test_module_executed_as_script_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert TEST_DATABASE_URL is not None  # narrows type for mypy; skipif already guards this
        monkeypatch.setenv("PGVECTOR_URL", TEST_DATABASE_URL)
        monkeypatch.setattr(sys, "argv", ["migrate.py"])

        with pytest.raises(SystemExit) as exc_info:
            runpy.run_module("penguincode_cli.db.migrate", run_name="__main__")

        assert exc_info.value.code == 0
