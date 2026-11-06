"""C2c: engines must 401 without the deployment bearer and honour rotation (AM11, AM16, AM18)."""

from __future__ import annotations

import pytest

KINDS = pytest.mark.parametrize("kind", ["vllm", "llamacpp"])


@KINDS
async def test_unauthenticated_request_rejected(running_engine, kind):
    """No bearer: 401."""
    async with running_engine.client(kind) as http:
        assert (await http.get("/v1/models")).status_code == 401


@KINDS
async def test_wrong_bearer_rejected_right_bearer_accepted(running_engine, kind):
    """A wrong bearer is 401; the deployment token is 200."""
    async with running_engine.client(kind) as http:
        wrong = await http.get("/v1/models", headers={"Authorization": "Bearer wrong"})
        right = await http.get(
            "/v1/models", headers={"Authorization": f"Bearer {running_engine.token}"}
        )
    assert (wrong.status_code, right.status_code) == (401, 200)


@KINDS
async def test_rotated_token_invalidates_old_bearer(running_engine, kind):
    """After rotation (and, for llama-server, the head restart) the old bearer is 401."""
    old = running_engine.token
    running_engine.set_token(kind, "rotated-token")
    running_engine.restart(kind)
    async with running_engine.client(kind) as http:
        assert (
            await http.get("/v1/models", headers={"Authorization": f"Bearer {old}"})
        ).status_code == 401
        new = await http.get("/v1/models", headers={"Authorization": "Bearer rotated-token"})
    assert new.status_code == 200


async def test_llamacpp_keeps_old_token_until_head_restart(running_engine):
    """llama-server loads the key once: rotation alone changes nothing until restart (AM18)."""
    running_engine.set_token("llamacpp", "rotated-token")
    async with running_engine.client("llamacpp") as http:
        still = await http.get(
            "/v1/models", headers={"Authorization": f"Bearer {running_engine.token}"}
        )
    assert still.status_code == 200
