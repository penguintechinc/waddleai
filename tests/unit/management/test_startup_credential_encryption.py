"""The management service refuses to boot without credential encryption.

regression: audit-2026-09-14 — the fail-closed encryption change initially
surfaced only at first credential write, as a 500. That is the wrong failure
mode for a deployment misconfiguration: 500s get lost in logs and the problem
appears at an arbitrary later moment, precisely when a secret was about to be
written. The check belongs at startup, where it is loud, immediate and
attributable to the deploy that caused it.

Two levels, because only one of them proves the real thing:
  * create_app() raises — fast, and pins the exception type and message.
  * a real subprocess importing asgi:app exits non-zero — proves the *process*
    dies, which is what actually stops a rollout. A unit-level raise that some
    outer try/except swallowed would still pass the first test alone.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from shared.security.credential_encryption import (
    KEY_ENV_VAR,
    PLAINTEXT_OPT_IN_ENV_VAR,
    CredentialEncryptionNotConfiguredError,
)

REPO = Path(__file__).resolve().parents[3]
MANAGEMENT = REPO / "services" / "management"


@pytest.fixture
def unconfigured(monkeypatch):
    """Remove both encryption env vars for the duration of one test.

    Function-scoped, so it overrides the session-scoped autouse fixture in
    conftest.py that supplies a key to every other management test.
    """
    monkeypatch.delenv(KEY_ENV_VAR, raising=False)
    monkeypatch.delenv(PLAINTEXT_OPT_IN_ENV_VAR, raising=False)


class TestCreateAppRefusesToStart:
    """create_app() is the startup gate."""

    def test_create_app_raises_without_a_key(self, unconfigured):
        """No key and no opt-in: the app factory raises instead of returning an app."""
        from services.management.app import create_app
        from services.management.app.config import TestingConfig

        with pytest.raises(CredentialEncryptionNotConfiguredError) as excinfo:
            create_app(TestingConfig)

        message = str(excinfo.value)
        assert KEY_ENV_VAR in message, "the error must name the env var to set"
        assert PLAINTEXT_OPT_IN_ENV_VAR in message, "the error must name the dev opt-in"
        assert "Refusing to start" in message

    def test_startup_check_runs_before_extensions(self, unconfigured, monkeypatch):
        """The gate fires before init_extensions, so a bad deploy fails fast.

        Ordering matters: init_extensions retries a DB connection ten times with
        a sleep between attempts, so a check placed after it would take well
        over a minute to surface a misconfiguration that is knowable instantly.
        """
        import services.management.app as app_module

        called = []
        monkeypatch.setattr(
            app_module, "init_extensions", lambda app: called.append("init_extensions")
        )

        from services.management.app.config import TestingConfig

        with pytest.raises(CredentialEncryptionNotConfiguredError):
            app_module.create_app(TestingConfig)

        assert called == [], "init_extensions ran before the encryption gate"


class TestRealProcessRefusesToStart:
    """The gate stops the actual service process, not just the factory."""

    @staticmethod
    def _boot(env_extra, tmp_path):
        """Import the real ASGI entrypoint in a subprocess; return (rc, stderr)."""
        env = {
            **os.environ,
            "DB_TYPE": "sqlite",
            "DATABASE_URL": f"sqlite:///{tmp_path}/startup.db",
            "FLASK_ENV": "testing",
            "CACHE_HOST": "",
            "REDIS_URL": "",
            "PYTHONPATH": str(REPO),
        }
        env.pop(KEY_ENV_VAR, None)
        env.pop(PLAINTEXT_OPT_IN_ENV_VAR, None)
        env.update(env_extra)
        proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell, no user input
            [sys.executable, "-c", "import asgi"],
            cwd=str(MANAGEMENT),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return proc.returncode, proc.stderr

    def test_process_exits_non_zero_without_a_key(self, tmp_path):
        """A real boot with no key dies, and says which env var to set."""
        rc, stderr = self._boot({}, tmp_path)
        assert rc != 0, "the service started despite having no credential-encryption key"
        assert KEY_ENV_VAR in stderr
        assert "CredentialEncryptionNotConfiguredError" in stderr

    def test_process_starts_with_a_key(self, tmp_path):
        """Control: the same boot succeeds once a key is supplied.

        Without this, the test above would pass just as well if the service
        were broken for some entirely unrelated reason.
        """
        rc, stderr = self._boot({KEY_ENV_VAR: "startup-test-key"}, tmp_path)
        assert rc == 0, f"service failed to boot even with a key configured:\n{stderr[-2000:]}"

    def test_process_starts_under_the_documented_dev_opt_in(self, tmp_path):
        """The plaintext opt-in is a real escape hatch, not a dead code path."""
        rc, stderr = self._boot({PLAINTEXT_OPT_IN_ENV_VAR: "1"}, tmp_path)
        assert rc == 0, f"the documented dev opt-in did not allow boot:\n{stderr[-2000:]}"


class TestRealServeCommandNeverServes:
    """The container's actual CMD, not a stand-in import.

    hypercorn EXITS 0 when the ASGI app fails to load -- it loads the app in a
    worker, logs the traceback and the master returns cleanly. So the exit code
    of the real serve command proves nothing, and any gate that checks it would
    be green on a service that never came up. What is actually guaranteed is
    that it never binds and never serves; assert that instead, and pin the
    exit-code quirk here so nobody later builds a check on top of it.

    In Kubernetes this still fails the deploy the right way: the process exits
    without binding, /healthz never answers, the pod never goes Ready and the
    rollout stalls rather than serving traffic.
    """

    @staticmethod
    def _serve(env_extra, tmp_path, port):
        """Run the image's real CMD; return (rc, stderr)."""
        env = {
            **os.environ,
            "DB_TYPE": "sqlite",
            "DATABASE_URL": f"sqlite:///{tmp_path}/serve.db",
            "FLASK_ENV": "testing",
            "CACHE_HOST": "",
            "REDIS_URL": "",
            "PYTHONPATH": str(REPO),
        }
        env.pop(KEY_ENV_VAR, None)
        env.pop(PLAINTEXT_OPT_IN_ENV_VAR, None)
        env.update(env_extra)
        proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell, no user input
            [
                sys.executable,
                "-m",
                "hypercorn",
                "asgi:app",
                "--bind",
                f"127.0.0.1:{port}",
                "--workers",
                "1",
            ],
            cwd=str(MANAGEMENT),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return proc.returncode, proc.stderr

    def test_serve_command_never_binds_without_a_key(self, tmp_path):
        """The real CMD terminates without ever serving, naming the missing var."""
        rc, stderr = self._serve({}, tmp_path, 18097)
        assert "Running on" not in stderr, "hypercorn bound a socket despite the failed gate"
        assert KEY_ENV_VAR in stderr
        assert "Refusing to start" in stderr
        # Documented, not asserted as a success signal: see the class docstring.
        assert rc == 0, (
            "hypercorn's exit code on a failed app load changed from 0 -- if it is "
            "now non-zero that is an improvement, but update this test and the "
            "class docstring rather than leaving the claim stale"
        )
