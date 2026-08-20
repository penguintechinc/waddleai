"""Shared pytest fixtures for contract tests: boot real hypercorn processes.

Launches the management and proxy services as real subprocesses (not
mocked/in-process) so contract tests exercise the actual ASGI apps end to
end, then tears them down at the end of the test session.
"""

import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait(url, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"server at {url} never became ready")


def _launch(module_app, port, cwd, db_dir=None, extra_env=None):
    # PYTHONPATH=REPO: both services import the top-level `shared` package
    # (e.g. `from shared.auth...`), which is only importable when the repo
    # root is on sys.path -- matches how the real container images set
    # PYTHONPATH=/app after COPYing shared/ alongside the service, and how
    # CI sets PYTHONPATH=${{ github.workspace }} for the same reason.
    #
    # cwd: service directory (for module imports, env setup).
    # db_dir: if provided, database file lives in this tmpdir (isolated per
    # session); otherwise DB is at cwd/contract.db (legacy, non-contract tests).
    db_path = db_dir if db_dir else cwd
    env = {
        **os.environ,
        "DB_TYPE": "sqlite",
        "DATABASE_URL": f"sqlite:///{db_path}/contract.db",
        "FLASK_ENV": "testing",
        "RELEASE_MODE": "false",
        "CACHE_HOST": "",
        "REDIS_URL": "",
        "PYTHONPATH": str(REPO),
    }
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen(  # noqa: S603 -- fixed argv launching the service under test, no shell, no user input
        ["hypercorn", module_app, "--bind", f"127.0.0.1:{port}", "--workers", "1"],  # noqa: S607 -- "hypercorn" is a fixed literal resolved from the test venv PATH, not user input
        cwd=str(cwd),
        env=env,
    )
    _wait(f"http://127.0.0.1:{port}/healthz")
    return proc


@pytest.fixture(scope="session")
def management_url(tmp_path_factory):
    """Boot a real management-service hypercorn process for the test session.

    Yields its base URL; the process is terminated once the session's tests
    finish. Seeds a deterministic admin password and webhook secret so
    contract tests can authenticate and verify webhook HMAC signatures.
    """
    db_tmpdir = tmp_path_factory.mktemp("mgmt_db")
    # Pre-migration this points at wsgi:app; after Task B-final it is asgi:app (same object).
    entry = "asgi:app" if (REPO / "services/management/asgi.py").exists() else "wsgi:app"
    # Deterministic secrets so contract tests can authenticate (admin login)
    # and produce a valid webhook HMAC signature.
    extra_env = {"ADMIN_INITIAL_PASSWORD": "admin123", "WEBHOOK_SECRET": "contract-webhook-secret"}
    proc = _launch(
        entry,
        _port := _free_port(),
        REPO / "services/management",
        db_dir=str(db_tmpdir),
        extra_env=extra_env,
    )
    yield f"http://127.0.0.1:{_port}"
    proc.terminate()


@pytest.fixture(scope="session")
def proxy_url(tmp_path_factory):
    """Boot a real proxy-service hypercorn process (stub upstream) for the session.

    Yields its base URL; the process is terminated once the session's tests
    finish. WADDLEAI_STUB_UPSTREAM=1 seeds deterministic org/user/api_key
    data and a test-only token-issuance endpoint (see test_proxy_contract.py).
    """
    db_tmpdir = tmp_path_factory.mktemp("proxy_db")
    proc = _launch(
        "apps.proxy_server.main:app",
        _port := _free_port(),
        REPO / "proxy",
        db_dir=str(db_tmpdir),
        extra_env={"WADDLEAI_STUB_UPSTREAM": "1"},
    )
    yield f"http://127.0.0.1:{_port}"
    proc.terminate()
