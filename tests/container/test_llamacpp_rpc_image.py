"""Plan F, F4: container contract for the llama.cpp RPC images (one per GPU vendor).

Run with the built refs exported: ``LLAMACPP_RPC_IMAGE_{CUDA,ROCM,SYCL}=<ref> pytest
tests/container/test_llamacpp_rpc_image.py``. An unset variable FAILS (never skips): a gate that
cannot run is not a pass.
"""

import os
import subprocess

import pytest

pytestmark = pytest.mark.container

VENDORS = ["cuda", "rocm", "sycl"]
REVISION_FORMAT = '{{index .Config.Labels "org.opencontainers.image.revision"}}'


def image(vendor: str) -> str:
    """The built image ref for ``vendor`` from its env variable; fails when unset."""
    env = f"LLAMACPP_RPC_IMAGE_{vendor.upper()}"
    value = os.environ.get(env)
    if not value:
        pytest.fail(f"{env} is unset; the container gate cannot run without the built image")
    return value


def run(img: str, *args: str, entrypoint: str) -> subprocess.CompletedProcess:
    """Run ``entrypoint args`` in the image (no GPU needed for --help / id)."""
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell
        ["docker", "run", "--rm", "--entrypoint", entrypoint, img, *args],  # noqa: S607
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip


@pytest.mark.parametrize("vendor", VENDORS)
def test_rpc_server_help(vendor):
    """rpc-server exists and documents its host, port and device flags."""
    r = run(image(vendor), "--help", entrypoint="/usr/local/bin/rpc-server")
    out = r.stdout + r.stderr
    assert r.returncode == 0 and "-p" in out and "-d" in out and "-H" in out


@pytest.mark.parametrize("vendor", VENDORS)
def test_runs_as_uid_1000(vendor):
    """The image runs rootless as uid 1000."""
    r = run(image(vendor), "-u", entrypoint="id")
    assert r.returncode == 0 and r.stdout.strip() == "1000"


@pytest.mark.parametrize("vendor", VENDORS)
def test_llama_server_has_openssl_and_api_key(vendor):
    """llama-server supports --api-key-file and TLS (built with OpenSSL)."""
    r = run(image(vendor), "--help", entrypoint="/usr/local/bin/llama-server")
    out = r.stdout + r.stderr
    assert "--api-key-file" in out and "--ssl-cert-file" in out and "--rpc" in out
    assert "--tensor-split" in out


@pytest.mark.parametrize("vendor", VENDORS)
def test_both_binaries_from_one_commit(vendor):
    """The image label records the single commit both binaries were built from."""
    r = subprocess.run(  # noqa: S603
        ["docker", "inspect", "--format", REVISION_FORMAT, image(vendor)],  # noqa: S607
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert r.returncode == 0 and len(r.stdout.strip()) == 40
