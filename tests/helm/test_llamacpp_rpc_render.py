"""Plan F, F8: llama.cpp RPC image refs and the mutual-auth env reach the management Deployment."""

from pathlib import Path

from tests.helm.conftest import render

BASE = str(Path(__file__).parent / "fixtures" / "vllm-ray-base.yaml")
DIGEST = "sha256:" + "b" * 64


def _env(set_values: dict[str, str] | None = None) -> dict[str, str | None]:
    """Environment of the management container for the given overrides."""
    docs = render(BASE, set_values)
    dep = [d for d in docs if d["kind"] == "Deployment" and "management" in d["metadata"]["name"]][
        0
    ]
    containers = dep["spec"]["template"]["spec"]["containers"]
    return {e["name"]: e.get("value") for e in containers[0]["env"]}


def test_rpc_images_and_mutual_auth_env_rendered():
    """A set digest renders the pinned ref; mutual auth follows the chart value."""
    env = _env({"fleet.security.mutualAuth": "false", "fleet.llamacpp.images.cuda.digest": DIGEST})
    assert env["WADDLEAI_FLEET_MUTUAL_AUTH"] == "false"
    assert env["WADDLEAI_LLAMACPP_RPC_IMAGE_CUDA"] == (
        f"ghcr.io/penguintechinc/waddleai/llamacpp-rpc:cuda@{DIGEST}"
    )


def test_unset_digest_renders_no_image_variable():
    """No digest -> no variable, so the API fails loudly instead of using an unpinned image."""
    env = _env({"fleet.llamacpp.images.rocm.digest": DIGEST})
    assert "WADDLEAI_LLAMACPP_RPC_IMAGE_ROCM" in env
    assert "WADDLEAI_LLAMACPP_RPC_IMAGE_CUDA" not in env
    assert "WADDLEAI_LLAMACPP_RPC_IMAGE_SYCL" not in env


def test_every_vendor_maps_to_its_own_variable():
    """Each vendor key renders its own variable name."""
    env = _env({f"fleet.llamacpp.images.{v}.digest": DIGEST for v in ("cuda", "rocm", "sycl")})
    refs = {k: v for k, v in env.items() if k.startswith("WADDLEAI_LLAMACPP_RPC_IMAGE_")}
    assert set(refs) == {f"WADDLEAI_LLAMACPP_RPC_IMAGE_{v}" for v in ("CUDA", "ROCM", "SYCL")}
    assert refs["WADDLEAI_LLAMACPP_RPC_IMAGE_SYCL"].endswith(f":sycl@{DIGEST}")
