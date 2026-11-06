"""Tests for the Ray Serve/Jobs REST client (Plan B6); httpx.MockTransport, no network."""
# ruff: noqa: S105, S106, S107  (dummy bearer tokens are the test fixtures)

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from shared.fleet.vllm_ray.ray_client import HttpRayServeClient, RayClientError
from tests.conformance._fake_ray import FakeRayServeClient

HEAD = "http://head:8265"


def _client(handler: Any, token: str | None = "tok", **kw: Any) -> HttpRayServeClient:
    """Build a client over a MockTransport handler."""
    return HttpRayServeClient(HEAD, token, transport=httpx.MockTransport(handler), **kw)


async def test_put_applications_sends_full_body_and_bearer() -> None:
    """PUT carries the full desired set and the bearer header."""
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(
            auth=req.headers["authorization"],
            body=json.loads(req.content),
            m=req.method,
            path=req.url.path,
        )
        return httpx.Response(200, json={})

    await _client(handler).put_applications({"applications": [{"name": "a"}]})
    assert seen == {
        "auth": "Bearer tok",
        "body": {"applications": [{"name": "a"}]},
        "m": "PUT",
        "path": "/api/serve/applications/",
    }


async def test_put_non_2xx_raises_without_token_in_message() -> None:
    """Non-2xx raises RayClientError with status+body, never the token."""
    client = _client(lambda r: httpx.Response(500, text="boom tok-echo"))
    with pytest.raises(RayClientError) as exc:
        await client.put_applications({"applications": []})
    msg = str(exc.value)
    assert "500" in msg and "boom" in msg and "tok" not in msg
    assert exc.value.status == 500


async def test_error_body_truncated_to_200_chars() -> None:
    """Error bodies are truncated to 200 characters."""
    client = _client(lambda r: httpx.Response(502, text="x" * 1000))
    with pytest.raises(RayClientError) as exc:
        await client.put_applications({"applications": []})
    assert len(exc.value.body) == 200


async def test_get_application_returns_status_and_not_found() -> None:
    """GET returns the app entry, or NOT_FOUND when absent."""
    body = {"applications": {"vllm-a": {"status": "DEPLOY_FAILED", "message": "OOM", "x": 1}}}

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET" and req.url.path == "/api/serve/applications/"
        return httpx.Response(200, json=body)

    client = _client(handler)
    assert await client.get_application("vllm-a") == {"status": "DEPLOY_FAILED", "message": "OOM"}
    assert await client.get_application("x") == {"status": "NOT_FOUND", "message": ""}


async def test_get_application_non_2xx_raises() -> None:
    """GET failure surfaces as RayClientError."""
    with pytest.raises(RayClientError):
        await _client(lambda r: httpx.Response(503, text="gcs down")).get_application("a")


async def test_delete_application_puts_remaining_config() -> None:
    """Delete is a PUT of the remaining config."""
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(m=req.method, body=json.loads(req.content))
        return httpx.Response(200)

    remaining = {"applications": [{"name": "keep"}]}
    await _client(handler).delete_application("gone", remaining)
    assert seen == {"m": "PUT", "body": remaining}


def _job_handler(statuses: list[str], logs: str, seen_auth: list[str] | None = None) -> Any:
    """Handler emulating POST /api/jobs/, GET job, GET logs."""
    polls = iter(statuses)

    def handler(req: httpx.Request) -> httpx.Response:
        if seen_auth is not None:
            seen_auth.append(req.headers.get("authorization", ""))
        if req.method == "POST" and req.url.path == "/api/jobs/":
            body = json.loads(req.content)
            assert body == {
                "entrypoint": "python3 -m shared.fleet.vllm_ray.inventory_job",
                "runtime_env": {},
            }
            return httpx.Response(200, json={"job_id": "j1", "submission_id": "j1"})
        if req.url.path == "/api/jobs/j1":
            return httpx.Response(200, json={"status": next(polls)})
        if req.url.path == "/api/jobs/j1/logs":
            return httpx.Response(200, json={"logs": logs})
        return httpx.Response(404)

    return handler


async def test_run_inventory_polls_until_succeeded_and_parses_payloads() -> None:
    """Polls RUNNING->SUCCEEDED, parses WAGPU_JSON lines, ignores noise."""
    logs = 'noise line\nWAGPU_JSON:{"node": "n1", "gpus": []}\nWAGPU_JSON:{"node": "n2"}\n'
    client = _client(_job_handler(["RUNNING", "SUCCEEDED"], logs), poll_interval_s=0)
    assert await client.run_inventory() == [{"node": "n1", "gpus": []}, {"node": "n2"}]


async def test_run_inventory_succeeded_without_payloads_returns_empty() -> None:
    """Zero parsed payloads on SUCCEEDED returns [] (caller decides)."""
    client = _client(_job_handler(["SUCCEEDED"], "only noise\n"), poll_interval_s=0)
    assert await client.run_inventory() == []


async def test_run_inventory_malformed_payload_raises() -> None:
    """A WAGPU_JSON line that is not JSON fails loudly."""
    client = _client(_job_handler(["SUCCEEDED"], "WAGPU_JSON:{not json\n"), poll_interval_s=0)
    with pytest.raises(RayClientError, match="malformed"):
        await client.run_inventory()


@pytest.mark.parametrize("final", ["FAILED", "STOPPED"])
async def test_run_inventory_failed_job_raises(final: str) -> None:
    """FAILED/STOPPED jobs raise RayClientError."""
    client = _client(_job_handler(["RUNNING", final], ""), poll_interval_s=0)
    with pytest.raises(RayClientError, match=final):
        await client.run_inventory()


async def test_run_inventory_deadline_raises() -> None:
    """A job stuck RUNNING hits the timeout_s*6 deadline."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(200, json={"submission_id": "j1"})
        return httpx.Response(200, json={"status": "RUNNING"})

    client = _client(handler, timeout_s=0.01, poll_interval_s=0)
    with pytest.raises(RayClientError, match="deadline"):
        await client.run_inventory()


async def test_run_inventory_submit_without_id_raises() -> None:
    """A submit response lacking submission_id is an error."""
    client = _client(lambda r: httpx.Response(200, json={}), poll_interval_s=0)
    with pytest.raises(RayClientError, match="submission_id"):
        await client.run_inventory()


def test_repr_masks_token() -> None:
    """Repr never contains the token."""
    client = _client(lambda r: httpx.Response(200), token="tok-secret")
    assert "tok-secret" not in repr(client)
    assert repr(client) == f"HttpRayServeClient(head_url={HEAD}, token=***)"


@pytest.mark.parametrize("url", ["ftp://head", "head:8265", ""])
def test_rejects_non_http_scheme(url: str) -> None:
    """Only http/https head URLs are accepted."""
    with pytest.raises(ValueError):
        HttpRayServeClient(url, "tok")


def test_trailing_slash_stripped() -> None:
    """Base URL has its trailing slash removed."""
    assert HttpRayServeClient("https://h:8265/", "t")._base == "https://h:8265"


async def test_timeout_is_applied() -> None:
    """Httpx timeouts surface as RayClientError and use the configured Timeout."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(RayClientError, match="timeout"):
        await _client(handler, timeout_s=0.5).put_applications({"applications": []})
    assert _client(handler, timeout_s=0.5)._timeout == httpx.Timeout(0.5)


async def test_transport_error_surfaces_as_ray_client_error() -> None:
    """Connection errors surface as RayClientError."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(RayClientError, match="ConnectError"):
        await _client(handler).get_application("a")


async def test_every_request_sends_bearer_from_token_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PUT, GET, POST, job poll and job logs each carry the file token."""
    tok = tmp_path / "token"
    tok.write_text("file-token\n")
    monkeypatch.delenv("WADDLEAI_RAY_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("RAY_AUTH_TOKEN_PATH", str(tok))
    seen: list[str] = []
    job = _job_handler(["SUCCEEDED"], "", seen)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/serve/applications/":
            seen.append(req.headers.get("authorization", ""))
            return httpx.Response(200, json={"applications": {}})
        return job(req)

    client = HttpRayServeClient.from_env(
        HEAD, transport=httpx.MockTransport(handler), poll_interval_s=0
    )
    await client.put_applications({"applications": []})
    await client.get_application("a")
    await client.run_inventory()
    assert len(seen) == 5  # PUT, GET, POST, poll, logs
    assert set(seen) == {"Bearer file-token"}


def test_env_token_beats_token_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WADDLEAI_RAY_AUTH_TOKEN wins over the token file."""
    tok = tmp_path / "token"
    tok.write_text("file-token")
    monkeypatch.setenv("RAY_AUTH_TOKEN_PATH", str(tok))
    monkeypatch.setenv("WADDLEAI_RAY_AUTH_TOKEN", "env-token")
    assert HttpRayServeClient.from_env(HEAD)._token == "env-token"


def test_default_token_path_is_plan_a_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env at all, the default path /etc/ray-auth/token is consulted."""
    monkeypatch.delenv("WADDLEAI_RAY_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("RAY_AUTH_TOKEN_PATH", raising=False)
    seen: list[str] = []
    real = Path.read_text

    def spy(self: Path, *a: Any, **k: Any) -> str:
        seen.append(str(self))
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", spy)
    with pytest.raises(RayClientError):
        HttpRayServeClient.from_env(HEAD)
    assert seen == ["/etc/ray-auth/token"]


@pytest.mark.parametrize("empty", [None, "", "  \n"])
def test_missing_token_fails_closed(empty: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    """No token (constructor) raises before any request is possible."""
    with pytest.raises(RayClientError, match="ray auth token unavailable"):
        HttpRayServeClient(HEAD, empty)


def test_from_env_no_env_no_file_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither env nor file present raises; no request sent."""
    monkeypatch.delenv("WADDLEAI_RAY_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("RAY_AUTH_TOKEN_PATH", str(tmp_path / "absent"))
    with pytest.raises(RayClientError, match="ray auth token unavailable"):
        HttpRayServeClient.from_env(
            HEAD, transport=httpx.MockTransport(lambda r: pytest.fail("sent"))
        )


def test_from_env_empty_file_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty token file is treated as unavailable."""
    tok = tmp_path / "token"
    tok.write_text("\n")
    monkeypatch.delenv("WADDLEAI_RAY_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("RAY_AUTH_TOKEN_PATH", str(tok))
    with pytest.raises(RayClientError, match="ray auth token unavailable"):
        HttpRayServeClient.from_env(HEAD)


async def test_token_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Full put/get/inventory at DEBUG never leaks the token (logs, errors, repr)."""
    secret = "SECRET-RAY-TOKEN"
    job = _job_handler(["SUCCEEDED"], "")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/serve/applications/":
            if req.method == "PUT":
                return httpx.Response(500, text=f"echo {secret}")
            return httpx.Response(200, json={"applications": {}})
        return job(req)

    client = _client(handler, token=secret, poll_interval_s=0)
    caplog.set_level(logging.DEBUG)
    errors: list[str] = []
    try:
        await client.put_applications({"applications": []})
    except RayClientError as exc:
        errors.append(str(exc))
    await client.get_application("a")
    await client.run_inventory()
    assert caplog.records  # something was logged at DEBUG
    assert secret not in caplog.text
    assert secret not in "".join(errors)
    assert secret not in repr(client)


async def test_fake_ray_client_roundtrip() -> None:
    """FakeRayServeClient implements the Protocol in memory."""
    fake = FakeRayServeClient()
    cfg = {"applications": [{"name": "a"}, {"name": "b"}]}
    await fake.put_applications(cfg)
    assert fake.apps == {"a": {"name": "a"}, "b": {"name": "b"}} and fake.puts == [cfg]
    assert (await fake.get_application("a"))["status"] == "NOT_FOUND"
    fake.statuses["a"] = {"status": "RUNNING", "message": ""}
    assert await fake.get_application("a") == {"status": "RUNNING", "message": ""}
    fake.fail_next_put = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await fake.put_applications(cfg)
    await fake.put_applications(cfg)  # raised once, then cleared
    await fake.delete_application("a", {"applications": [{"name": "b"}]})
    assert list(fake.apps) == ["b"] and len(fake.puts) == 3
    fake.fail_next_get = RuntimeError("get boom")
    with pytest.raises(RuntimeError):
        await fake.get_application("a")
    fake.inventory_payloads = [{"node": "n"}]
    assert await fake.run_inventory() == [{"node": "n"}]


def _nodes_body(rows: list[dict[str, Any]], **page: Any) -> dict[str, Any]:
    """Build a Ray 2.60 /api/v0/nodes reply (rest_response envelope around ListApiResponse)."""
    result = {"total": len(rows), "num_after_truncation": len(rows), "num_filtered": len(rows)}
    result.update(page)
    return {"result": True, "msg": "", "data": {"result": {**result, "result": rows}}}


async def test_list_alive_node_ids_returns_only_alive_with_bearer() -> None:
    """GET /api/v0/nodes is authenticated and DEAD nodes are dropped."""
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(auth=req.headers["authorization"], path=req.url.path, q=dict(req.url.params))
        rows = [
            {"node_id": "aa", "state": "ALIVE"},
            {"node_id": "bb", "state": "DEAD"},
            {"node_id": "cc", "state": "ALIVE"},
        ]
        return httpx.Response(200, json=_nodes_body(rows))

    assert await _client(handler).list_alive_node_ids() == ["aa", "cc"]
    assert seen["auth"] == "Bearer tok"
    assert seen["path"] == "/api/v0/nodes"
    assert seen["q"] == {"limit": "10000"}


@pytest.mark.parametrize(
    "body",
    [
        {"result": False, "msg": "x", "data": {}},
        {"result": True, "msg": "", "data": {}},
        {"result": True, "msg": "", "data": {"result": {"result": "nope"}}},
        _nodes_body([{"node_id": "a", "state": "ALIVE"}], num_filtered=5),
        _nodes_body([{"node_id": "a", "state": "ALIVE"}], partial_failure_warning="boom"),
    ],
)
async def test_list_alive_node_ids_unusable_reply_fails_closed(body: dict[str, Any]) -> None:
    """Malformed, partial or truncated lists raise rather than act as authoritative."""
    client = _client(lambda r: httpx.Response(200, json=body))
    with pytest.raises(RayClientError):
        await client.list_alive_node_ids()


async def test_list_alive_node_ids_http_error_raises() -> None:
    """A 401 from the head surfaces as RayClientError."""
    client = _client(lambda r: httpx.Response(401, text="no"))
    with pytest.raises(RayClientError, match="401"):
        await client.list_alive_node_ids()
