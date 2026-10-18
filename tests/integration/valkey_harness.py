"""Real-Valkey test harness: one Docker container per session, no fakeredis.

The Lua lease/reserve/reconcile scripts can only be proven against a real server (fakeredis
does not implement our scripts' semantics faithfully). Skips when Docker is missing unless
``REQUIRE_VALKEY_TESTS=1`` (set by ``make test-valkey`` and CI), in which case it FAILS --
a silently-skipped gate is the same defect as ``|| true`` (critical-rules Verification Integrity).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
import redis

from tests.e2e.conftest import _free_port

#: Pinned by SHA256 digest (tag valkey/valkey:8.0.1-bookworm); env override for experiments.
VALKEY_IMAGE = os.environ.get(
    "WADDLEAI_TEST_VALKEY_IMAGE",
    "valkey/valkey@sha256:c5d4f082b76d433a38274ebb38a43cff19829a062928bdcc45611135ef54e608",
)
REQUIRE_ENV = "REQUIRE_VALKEY_TESTS"


@dataclass(frozen=True)
class ValkeyServer:
    """Connection details of the session's Valkey container."""

    url: str
    port: int


def unavailable(reason: str) -> None:
    """Fail when Valkey tests are required, otherwise skip -- never silently pass."""
    if os.environ.get(REQUIRE_ENV) == "1":
        pytest.fail(f"{REQUIRE_ENV}=1 but {reason}", pytrace=False)
    pytest.skip(reason)


def _wait_ready(url: str, timeout_s: float = 30.0) -> bool:
    """Poll PING until the server answers or the timeout elapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            client = redis.Redis.from_url(url)
            try:
                if client.ping():
                    return True
            finally:
                client.close()
        except redis.RedisError:
            pass
        time.sleep(0.2)
    return False


def start_valkey() -> Iterator[ValkeyServer]:
    """Start a throwaway Valkey container, yield its address, always remove it."""
    if shutil.which("docker") is None:
        unavailable("docker is not available -- quota Lua tests need a real Valkey")
    port = _free_port()
    name = f"waddleai-quota-valkey-{uuid.uuid4().hex[:8]}"
    argv = [
        "docker",
        "run",
        "--rm",
        "-d",
        "--name",
        name,
        "-p",
        f"127.0.0.1:{port}:6379",
        VALKEY_IMAGE,
    ]
    try:
        subprocess.run(argv, check=True, capture_output=True, timeout=120, text=True)  # noqa: S603
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        unavailable(f"could not start {VALKEY_IMAGE}: {exc}")
    url = f"redis://127.0.0.1:{port}/0"
    try:
        if not _wait_ready(url):
            unavailable(f"{VALKEY_IMAGE} did not answer PING within 30s")
        yield ValkeyServer(url=url, port=port)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)  # noqa: S603,S607
