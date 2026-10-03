"""Tests for `KnowledgeClient`'s O10-a async index-job additions: `index()`/
`index_code()`'s `wait`/`poll_interval` contract, `get_index_job`,
`wait_for_index_job`, `list_index_jobs`, and the `RESOURCE_EXHAUSTED` ->
`KnowledgeQueueFullError` mapping.

Mirrors `tests/test_knowledge_client.py`'s fully-mocked-stub style -- no real
gRPC channel/server involved.

# regression: penguincode-index-job-queue (O10-a -- load leveling for Index/IndexCode)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import grpc
import pytest
from grpc.aio import Metadata

from penguincode_cli.client.knowledge_client import (
    KnowledgeClient,
    KnowledgeJobFailedError,
    KnowledgeQueueFullError,
)
from penguincode_cli.config.settings import ServerConfig
from penguincode_cli.proto import (
    IndexCodeResponse,
    IndexResponse,
    IndexStatusResponse,
    ListIndexJobsResponse,
)
from penguincode_cli.proto import JobState as ProtoJobState
from penguincode_cli.proto import JobType as ProtoJobType

_AUTH_METADATA = [("authorization", "Bearer test-jwt")]


class _FakeStub:
    def __init__(self) -> None:
        self.Index = AsyncMock()
        self.IndexCode = AsyncMock()
        self.IndexStatus = AsyncMock()
        self.ListIndexJobs = AsyncMock()


def _rpc_error(code: grpc.StatusCode, details: str = "boom") -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(code, Metadata(), Metadata(), details=details)


def _client(monkeypatch: pytest.MonkeyPatch, stub: _FakeStub) -> KnowledgeClient:
    monkeypatch.setattr(
        "penguincode_cli.client.knowledge_client.KnowledgeServiceStub", lambda channel: stub
    )
    token_provider = AsyncMock()
    token_provider.get_auth_metadata = AsyncMock(return_value=_AUTH_METADATA)
    server_config = ServerConfig(host="pc-server.internal", port=50051)
    return KnowledgeClient(server_config, token_provider=token_provider, channel=object())


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every poll loop in this file uses a 0-second interval and a pre-scripted
    sequence of `IndexStatus` responses -- no test here should ever actually
    sleep for wall-clock time."""

    async def _instant_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("penguincode_cli.client.knowledge_client.asyncio.sleep", _instant_sleep)


class TestIndexResourceExhausted:
    async def test_queue_full_raises_knowledge_queue_full_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        stub.Index.side_effect = _rpc_error(grpc.StatusCode.RESOURCE_EXHAUSTED, "queue full")
        client = _client(monkeypatch, stub)

        with pytest.raises(KnowledgeQueueFullError, match="queue full"):
            await client.index(language="python", doc_contents=["x"])


class TestIndexWaitTrue:
    async def test_polls_index_status_until_succeeded_and_returns_final_chunks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        stub.Index.return_value = IndexResponse(
            chunks_indexed=0, job_id="job-1", state=ProtoJobState.JOB_STATE_QUEUED
        )
        stub.IndexStatus.side_effect = [
            IndexStatusResponse(job_id="job-1", state=ProtoJobState.JOB_STATE_RUNNING),
            IndexStatusResponse(
                job_id="job-1",
                state=ProtoJobState.JOB_STATE_SUCCEEDED,
                chunks_done=7,
                chunks_total=7,
            ),
        ]
        client = _client(monkeypatch, stub)

        result = await client.index(language="python", doc_contents=["a", "b"])

        assert result.job_id == "job-1"
        assert result.state == "succeeded"
        assert result.chunks_indexed == 7
        assert stub.IndexStatus.await_count == 2

    async def test_polled_failure_raises_knowledge_job_failed_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        stub.Index.return_value = IndexResponse(
            chunks_indexed=0, job_id="job-2", state=ProtoJobState.JOB_STATE_QUEUED
        )
        stub.IndexStatus.return_value = IndexStatusResponse(
            job_id="job-2", state=ProtoJobState.JOB_STATE_FAILED, error="ollama unreachable"
        )
        client = _client(monkeypatch, stub)

        with pytest.raises(KnowledgeJobFailedError, match="ollama unreachable"):
            await client.index(language="python", doc_contents=["a"])


class TestIndexWaitFalse:
    async def test_returns_immediately_without_polling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        stub.Index.return_value = IndexResponse(
            chunks_indexed=0, job_id="job-3", state=ProtoJobState.JOB_STATE_QUEUED
        )
        client = _client(monkeypatch, stub)

        result = await client.index(language="python", doc_contents=["a"], wait=False)

        assert result.job_id == "job-3"
        assert result.state == "queued"
        assert result.chunks_indexed == 0
        stub.IndexStatus.assert_not_awaited()


class TestIndexCodeQueued:
    async def test_waits_and_decodes_node_edge_counts_from_result_struct(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        stub.IndexCode.return_value = IndexCodeResponse(
            indexed=False,
            node_count=0,
            edge_count=0,
            job_id="job-4",
            state=ProtoJobState.JOB_STATE_QUEUED,
        )
        status = IndexStatusResponse(job_id="job-4", state=ProtoJobState.JOB_STATE_SUCCEEDED)
        status.result.update({"indexed": True, "node_count": 5, "edge_count": 8})
        stub.IndexStatus.return_value = status
        client = _client(monkeypatch, stub)

        result = await client.index_code(root_path="/repo")

        assert result is not None
        assert result.node_count == 5
        assert result.edge_count == 8

    async def test_flag_off_result_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _FakeStub()
        stub.IndexCode.return_value = IndexCodeResponse(
            indexed=False,
            node_count=0,
            edge_count=0,
            job_id="job-5",
            state=ProtoJobState.JOB_STATE_QUEUED,
        )
        status = IndexStatusResponse(job_id="job-5", state=ProtoJobState.JOB_STATE_SUCCEEDED)
        status.result.update({"indexed": False, "node_count": 0, "edge_count": 0})
        stub.IndexStatus.return_value = status
        client = _client(monkeypatch, stub)

        result = await client.index_code(root_path="/repo")

        assert result is None


class TestGetIndexJobAndListIndexJobs:
    async def test_get_index_job_decodes_every_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _FakeStub()
        stub.IndexStatus.return_value = IndexStatusResponse(
            job_id="job-6",
            job_type=ProtoJobType.JOB_TYPE_INDEX_DOCS,
            state=ProtoJobState.JOB_STATE_RUNNING,
            chunks_done=2,
            chunks_total=5,
            error="",
            created_at="2026-10-03T00:00:00",
            updated_at="2026-10-03T00:00:05",
        )
        client = _client(monkeypatch, stub)

        status = await client.get_index_job("job-6")

        assert status.job_id == "job-6"
        assert status.job_type == "index_docs"
        assert status.state == "running"
        assert status.chunks_done == 2
        assert status.chunks_total == 5

    async def test_list_index_jobs_decodes_every_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from penguincode_cli.proto import IndexJobSummary

        stub = _FakeStub()
        stub.ListIndexJobs.return_value = ListIndexJobsResponse(
            jobs=[
                IndexJobSummary(
                    job_id="job-7",
                    job_type=ProtoJobType.JOB_TYPE_INDEX_CODE,
                    state=ProtoJobState.JOB_STATE_SUCCEEDED,
                    chunks_done=0,
                    chunks_total=0,
                )
            ]
        )
        client = _client(monkeypatch, stub)

        jobs = await client.list_index_jobs(limit=10)

        assert len(jobs) == 1
        assert jobs[0].job_id == "job-7"
        assert jobs[0].job_type == "index_code"
        assert jobs[0].state == "succeeded"
        request = stub.ListIndexJobs.call_args.args[0]
        assert request.limit == 10


class TestWaitForIndexJobPollInterval:
    async def test_custom_poll_interval_is_passed_to_asyncio_sleep(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        stub.IndexStatus.side_effect = [
            IndexStatusResponse(job_id="job-8", state=ProtoJobState.JOB_STATE_QUEUED),
            IndexStatusResponse(job_id="job-8", state=ProtoJobState.JOB_STATE_SUCCEEDED),
        ]
        client = _client(monkeypatch, stub)
        sleeps: list[float] = []

        async def _recording_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(
            "penguincode_cli.client.knowledge_client.asyncio.sleep", _recording_sleep
        )

        await client.wait_for_index_job("job-8", poll_interval=0.01)

        assert sleeps == [0.01]
