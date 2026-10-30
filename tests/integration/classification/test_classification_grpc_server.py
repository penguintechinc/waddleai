"""Integration: real grpc.aio server + channel + interceptor with a stub engine (T10)."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import grpc
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parents[2] / "unit" / "classification"))
import auth_fakes as af  # noqa: E402

from services.classification.app.config import ClassificationConfig  # noqa: E402
from services.classification.app.grpc_gen import classification_pb2 as pb  # noqa: E402
from services.classification.app.grpc_gen import classification_pb2_grpc as pb_grpc  # noqa: E402
from services.classification.app.grpc_server import bound_port, build_grpc_server  # noqa: E402
from services.classification.app.servicer import ClassificationServicer  # noqa: E402


def _req() -> Any:
    """Analyze request for 'Alice here'."""
    return pb.AnalyzeRequest(api_version="v1", text="Alice here", language="en", org_public_id="o")


async def _serve(engine: af.FakeEngine) -> tuple[grpc.aio.Server, Any, grpc.aio.Channel]:
    """Start a real server around ``engine`` and connect a client."""
    cfg = ClassificationConfig(grpc_port=0)
    servicer = ClassificationServicer(
        engine, af.FakeCache(), af.FakeGate(), af.FakePolicy(), cfg.max_text_bytes
    )
    server = await build_grpc_server(cfg, servicer, af.make_verifier())
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{bound_port(server)}")
    return server, pb_grpc.ClassificationServiceStub(channel), channel


@pytest_asyncio.fixture
async def served() -> AsyncIterator[Any]:
    """Real server with an instant stub engine."""
    server, stub, channel = await _serve(af.FakeEngine())
    yield stub
    await channel.close()
    await server.stop(0)


MD = (("authorization", f"Bearer {af.make_token()}"),)


async def test_analyze_over_real_grpc_returns_spans_with_stub_engine(served: Any) -> None:
    """Authenticated Analyze over a real channel returns the stub engine's span."""
    resp = await served.Analyze(_req(), metadata=MD)
    assert resp.ner_status == pb.NER_STATUS_OK
    assert [(s.start_cp, s.end_cp) for s in resp.spans] == [(0, 5)]
    assert resp.backend == "presidio"


async def test_graceful_shutdown_completes_inflight() -> None:
    """stop(grace) lets an in-flight RPC finish while refusing new ones."""
    engine = af.FakeEngine(delay=0.5)
    server, stub, channel = await _serve(engine)

    async def call() -> Any:
        return await stub.Analyze(_req(), metadata=MD)

    inflight = asyncio.create_task(call())
    await asyncio.wait_for(engine.started_event.wait(), 5)
    stopper = asyncio.create_task(server.stop(5))
    await asyncio.sleep(0.1)
    with pytest.raises(grpc.aio.AioRpcError):
        await stub.Analyze(_req(), metadata=MD, timeout=1)
    resp = await inflight
    await stopper
    await channel.close()
    assert resp.ner_status == pb.NER_STATUS_OK
    assert len(resp.spans) == 1
