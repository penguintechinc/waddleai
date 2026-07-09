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


def _launch(module_app, port, cwd, extra_env=None):
    # PYTHONPATH=REPO: both services import the top-level `shared` package
    # (e.g. `from shared.auth...`), which is only importable when the repo
    # root is on sys.path -- matches how the real container images set
    # PYTHONPATH=/app after COPYing shared/ alongside the service, and how
    # CI sets PYTHONPATH=${{ github.workspace }} for the same reason.
    env = {**os.environ, "DB_TYPE": "sqlite",
           "DATABASE_URL": f"sqlite:///{cwd}/contract.db",
           "FLASK_ENV": "testing", "RELEASE_MODE": "false",
           "CACHE_HOST": "", "REDIS_URL": "",
           "PYTHONPATH": str(REPO)}
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen(
        ["hypercorn", module_app, "--bind", f"127.0.0.1:{port}", "--workers", "1"],
        cwd=str(cwd), env=env,
    )
    _wait(f"http://127.0.0.1:{port}/healthz")
    return proc


@pytest.fixture(scope="session")
def management_url(tmp_path_factory):
    cwd = tmp_path_factory.mktemp("mgmt")
    # Pre-migration this points at wsgi:app; after Task B-final it is asgi:app (same object).
    entry = "asgi:app" if (REPO / "services/management/asgi.py").exists() else "wsgi:app"
    proc = _launch(entry, _port := _free_port(), REPO / "services/management")
    yield f"http://127.0.0.1:{_port}"
    proc.terminate()


@pytest.fixture(scope="session")
def proxy_url(tmp_path_factory):
    proc = _launch("apps.proxy_server.main:app", _port := _free_port(), REPO / "proxy",
                   {"WADDLEAI_STUB_UPSTREAM": "1"})
    yield f"http://127.0.0.1:{_port}"
    proc.terminate()
