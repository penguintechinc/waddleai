"""The default canary against the REAL BearerKeyMiddleware (no mocked auth outcome)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from shared.fleet.vllm_ray.backend import _default_canary
from shared.fleet.vllm_ray.serve_app import BearerKeyMiddleware

SLUG = "devstral-small"
URL = f"http://serve/d/{SLUG}/v1"


async def _openai_app(scope: Any, receive: Any, send: Any) -> None:
    """Tiny OpenAI-shaped ASGI app: /models lists the served id, chat returns 200."""
    if scope["path"].endswith("/models"):
        body = json.dumps({"data": [{"id": "served-m"}]}).encode()
    else:
        body = b"{}"
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


def _transport(tmp_path: Path, key: str | None) -> httpx.ASGITransport:
    """Real middleware over the tiny app, key file written as the kubelet would."""
    if key is not None:
        (tmp_path / SLUG).write_text(key)
    app = BearerKeyMiddleware(_openai_app, slug=SLUG, keys_dir=str(tmp_path))
    return httpx.ASGITransport(app=app)


async def test_canary_with_correct_key_passes(tmp_path: Path) -> None:
    """The per-deployment key authenticates through the real middleware."""
    t = _transport(tmp_path, "s3cret")
    assert await _default_canary(URL, "served-m", "s3cret", transport=t) is True


@pytest.mark.parametrize("presented", ["wrong", ""])
async def test_canary_with_wrong_or_empty_key_fails(tmp_path: Path, presented: str) -> None:
    """A wrong/empty key is a real 401 from the middleware -> canary False."""
    t = _transport(tmp_path, "s3cret")
    assert await _default_canary(URL, "served-m", presented, transport=t) is False


async def test_canary_when_key_file_missing_fails_closed(tmp_path: Path) -> None:
    """No key file in the Secret mount: middleware fails closed, canary False."""
    t = _transport(tmp_path, None)
    assert await _default_canary(URL, "served-m", "anything", transport=t) is False
