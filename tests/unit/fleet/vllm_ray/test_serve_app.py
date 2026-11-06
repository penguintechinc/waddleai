"""Tests for the authenticated Serve app wrapper and bearer middleware (Plan B5).

Ray/FastAPI are not importable in the unit env, so the middleware is driven as
plain ASGI and the Ray modules are faked via ``sys.modules``.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from shared.fleet.vllm_ray.serve_app import (
    BearerKeyMiddleware,
    build_authenticated_openai_app,
)

LLM: dict[str, Any] = {"model_loading_config": {"model_id": "m", "model_source": "/models/m"}}


async def _inner(scope: Any, receive: Any, send: Any) -> None:
    """Inner."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def _call(mw: Any, auth: str | None, scope_type: str = "http") -> list[dict[str, Any]]:
    """Call."""
    headers = [(b"authorization", auth.encode())] if auth is not None else []
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        """Receive."""
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg: dict[str, Any]) -> None:
        """Send."""
        sent.append(msg)

    await mw({"type": scope_type, "headers": headers, "path": "/v1/models"}, receive, send)
    return sent


def _status(sent: list[dict[str, Any]]) -> int:
    """Status."""
    return int(sent[0]["status"])


async def test_middleware_reads_key_from_keys_dir(tmp_path: Path) -> None:
    """Middleware reads key from keys dir."""
    (tmp_path / "s").write_text("tok-1\n")
    mw = BearerKeyMiddleware(_inner, slug="s", keys_dir=str(tmp_path))
    assert _status(await _call(mw, "Bearer tok-1")) == 200
    assert _status(await _call(mw, "Bearer wrong")) == 401
    assert _status(await _call(mw, None)) == 401
    assert _status(await _call(mw, "Basic tok-1")) == 401
    assert _status(await _call(mw, "Bearer ")) == 401


async def test_rotation_picks_up_new_file(tmp_path: Path) -> None:
    """Rotation picks up new file."""
    f = tmp_path / "s"
    f.write_text("old-token")
    mw = BearerKeyMiddleware(_inner, slug="s", keys_dir=str(tmp_path))
    assert _status(await _call(mw, "Bearer old-token")) == 200
    f.write_text("new-token")
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert _status(await _call(mw, "Bearer new-token")) == 200
    assert _status(await _call(mw, "Bearer old-token")) == 401


async def test_unchanged_mtime_uses_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unchanged mtime uses cache."""
    (tmp_path / "s").write_text("tok")
    mw = BearerKeyMiddleware(_inner, slug="s", keys_dir=str(tmp_path))
    await _call(mw, "Bearer tok")
    reads = 0
    real = Path.read_bytes

    def counting(self: Path) -> bytes:
        """Counting."""
        nonlocal reads
        reads += 1
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    assert _status(await _call(mw, "Bearer tok")) == 200
    assert reads == 0


async def test_missing_key_file_returns_401(tmp_path: Path) -> None:
    """Missing key file returns 401."""
    mw = BearerKeyMiddleware(_inner, slug="s", keys_dir=str(tmp_path))
    assert _status(await _call(mw, "Bearer anything")) == 401


async def test_key_file_removed_after_cache_returns_401(tmp_path: Path) -> None:
    """Key file removed after cache returns 401."""
    f = tmp_path / "s"
    f.write_text("tok")
    mw = BearerKeyMiddleware(_inner, slug="s", keys_dir=str(tmp_path))
    assert _status(await _call(mw, "Bearer tok")) == 200
    f.unlink()
    assert _status(await _call(mw, "Bearer tok")) == 401


async def test_empty_key_file_returns_401(tmp_path: Path) -> None:
    """Empty key file returns 401."""
    (tmp_path / "s").write_text("\n")
    mw = BearerKeyMiddleware(_inner, slug="s", keys_dir=str(tmp_path))
    assert _status(await _call(mw, "Bearer ")) == 401
    assert _status(await _call(mw, "Bearer x")) == 401


async def test_unreadable_key_file_returns_401(tmp_path: Path) -> None:
    """Unreadable key file returns 401."""
    (tmp_path / "s").mkdir()  # a directory: stat ok, read fails
    mw = BearerKeyMiddleware(_inner, slug="s", keys_dir=str(tmp_path))
    assert _status(await _call(mw, "Bearer x")) == 401


async def test_websocket_is_closed_never_open(tmp_path: Path) -> None:
    """Websocket is closed never open."""
    (tmp_path / "s").write_text("tok")
    mw = BearerKeyMiddleware(_inner, slug="s", keys_dir=str(tmp_path))
    sent = await _call(mw, "Bearer wrong", scope_type="websocket")
    assert sent == [{"type": "websocket.close", "code": 1008}]


async def test_lifespan_passes_through(tmp_path: Path) -> None:
    """Lifespan passes through."""
    mw = BearerKeyMiddleware(_inner, slug="s", keys_dir=str(tmp_path))
    sent = await _call(mw, None, scope_type="lifespan")
    assert _status(sent) == 200


@pytest.mark.parametrize("slug", ["../x", "a/b", "", ".", ".."])
def test_bad_slug_rejected(slug: str, tmp_path: Path) -> None:
    """Bad slug rejected."""
    with pytest.raises(ValueError):
        BearerKeyMiddleware(_inner, slug=slug, keys_dir=str(tmp_path))


def _fake_ray(monkeypatch: pytest.MonkeyPatch, *, call_init: bool = True) -> dict[str, Any]:
    """Fake ray."""
    rec: dict[str, Any] = {"calls": [], "middleware": []}

    class FakeApp:
        def add_middleware(self, cls: Any, **kw: Any) -> None:
            """Add middleware."""
            rec["middleware"].append((cls, kw))

    ingress = types.ModuleType("ray.llm._internal.serve.core.ingress.ingress")
    ingress.init = lambda: FakeApp()  # type: ignore[attr-defined]

    def build_openai_app(args: dict[str, Any]) -> str:
        """Build openai app."""
        rec["calls"].append(args)
        if call_init:
            ingress.init()  # type: ignore[attr-defined]  # builder's make_fastapi_ingress does this
        return "APP"

    llm = types.ModuleType("ray.serve.llm")
    llm.build_openai_app = build_openai_app  # type: ignore[attr-defined]
    rec["ingress"] = ingress
    rec["orig_init"] = ingress.init  # type: ignore[attr-defined]
    for name in (
        "ray",
        "ray.serve",
        "ray.llm",
        "ray.llm._internal",
        "ray.llm._internal.serve",
        "ray.llm._internal.serve.core",
        "ray.llm._internal.serve.core.ingress",
    ):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "ray.serve.llm", llm)
    monkeypatch.setitem(sys.modules, "ray.llm._internal.serve.core.ingress.ingress", ingress)
    return rec


def test_builder_passes_llm_configs_list_to_build_openai_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Builder passes llm configs list to build openai app."""
    rec = _fake_ray(monkeypatch)
    out = build_authenticated_openai_app(llm_config=LLM, slug="s", keys_dir=str(tmp_path))
    assert out == "APP"
    # Ray 2.60: build_openai_app takes ONE dict (LLMServingArgs shape).
    assert rec["calls"] == [{"llm_configs": [LLM]}]


def test_builder_attaches_middleware_to_ingress_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Builder attaches middleware to ingress app."""
    rec = _fake_ray(monkeypatch)
    build_authenticated_openai_app(llm_config=LLM, slug="s", keys_dir=str(tmp_path))
    assert rec["middleware"] == [(BearerKeyMiddleware, {"slug": "s", "keys_dir": str(tmp_path)})]
    assert rec["ingress"].init is rec["orig_init"]  # patch is restored


def test_builder_restores_init_when_build_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Builder restores init when build raises."""
    rec = _fake_ray(monkeypatch)
    sys.modules["ray.serve.llm"].build_openai_app = lambda a: (_ for _ in ()).throw(KeyError("x"))  # type: ignore[attr-defined]
    with pytest.raises(KeyError):
        build_authenticated_openai_app(llm_config=LLM, slug="s", keys_dir=str(tmp_path))
    assert rec["ingress"].init is rec["orig_init"]


def test_builder_fails_closed_when_no_ingress_app_was_built(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Builder fails closed when no ingress app was built."""
    _fake_ray(monkeypatch, call_init=False)  # e.g. direct-streaming mode skips the ingress
    with pytest.raises(RuntimeError, match="auth middleware"):
        build_authenticated_openai_app(llm_config=LLM, slug="s", keys_dir=str(tmp_path))
