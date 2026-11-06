"""Slug validation, derivation and traversal rejection."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from shared.fleet.vllm_ray.serve_config import build_serve_app
from shared.fleet.vllm_ray.slug import derive_slug, validate_slug

BAD = ["", "..", "../x", "a/b", "A", "-a", "a_b", "a" * 64, "a b", "a\x00", "."]


@pytest.mark.parametrize("slug", BAD)
def test_validate_slug_rejects(slug: str) -> None:
    """Anything outside ^[a-z0-9][a-z0-9-]{0,62}$ is refused."""
    with pytest.raises(ValueError):
        validate_slug(slug)


def test_validate_slug_accepts_boundaries() -> None:
    """1 and 63 char slugs are valid."""
    assert validate_slug("a") == "a"
    assert validate_slug("a" * 63)


def _dep(slug: str) -> Any:
    return SimpleNamespace(
        slug=slug, serve_app_name="app", served_model_name="m", tensor_parallel=1,
        pipeline_parallel=1, quantization=0, dtype="bfloat16", max_model_len=8192,
        status=1, replicas_min=1, replicas_max=1, gpu_class="nvidia-a100-80g",
    )  # fmt: skip


@pytest.mark.parametrize("slug", ["../etc", "a/b", "A"])
def test_build_serve_app_rejects_bad_slug(slug: str) -> None:
    """build_serve_app refuses a slug that would form a route/path."""
    with pytest.raises(ValueError):
        build_serve_app(_dep(slug), "/models/x", {})


@pytest.mark.parametrize(
    "path", ["/models/../etc/passwd", "/models/a/../../x", "/models/..", "/etc"]
)
def test_build_serve_app_rejects_traversal_model_path(path: str) -> None:
    """A model path escaping /models/ (or containing '..') is refused."""
    with pytest.raises(ValueError):
        build_serve_app(_dep("ok"), path, {})


def test_build_serve_app_normalises_model_path() -> None:
    """Redundant separators normalise to a clean path under /models/."""
    app = build_serve_app(_dep("ok"), "/models//ok/./w", {})
    assert app["args"]["llm_config"]["model_loading_config"]["model_source"] == "/models/ok/w"


def test_same_name_in_two_orgs_gets_different_slugs() -> None:
    """Slug is globally unique: the public_id suffix differs per deployment."""
    a = derive_slug("My Model!", uuid.uuid4())
    b = derive_slug("My Model!", uuid.uuid4())
    assert a != b and a.startswith("my-model-")
    validate_slug(a)


def test_derive_slug_truncates_name_and_handles_empty() -> None:
    """Name part is capped at 40; an all-symbol name falls back to the id alone."""
    pid = uuid.UUID(int=0xABCDEF)
    long = derive_slug("x" * 100, pid)
    assert long == "x" * 40 + "-" + pid.hex[:12]
    assert derive_slug("!!!", pid) == pid.hex[:12]


async def test_deploy_rejects_invalid_slug() -> None:
    """VllmRayBackend.deploy validates the slug before touching the store path."""
    from shared.fleet.vllm_ray.backend import VllmRayBackend

    b = VllmRayBackend.__new__(VllmRayBackend)
    b.config = {"model_store_path": "/models"}
    b._check_link_auth = lambda dep: None  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="invalid deployment slug"):
        await b.deploy(SimpleNamespace(slug="../x"))
