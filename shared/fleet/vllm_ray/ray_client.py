"""Async REST client for the Ray head: Serve applications API and Jobs API.

Endpoints verified against Ray 2.60.0 (dashboard ``serve_head.py`` / ``job_head.py``):
``PUT|GET /api/serve/applications/`` (GET returns ``{"applications": {name: {...}}}``),
``POST /api/jobs/`` -> ``{"job_id", "submission_id"}``, ``GET /api/jobs/{id}`` ->
``{"status": PENDING|RUNNING|STOPPED|SUCCEEDED|FAILED, ...}``, ``GET /api/jobs/{id}/logs``
-> ``{"logs": str}``. Ray token auth (``RAY_AUTH_MODE=token``) is mandatory (AM11/AM12):
the client fails closed with no token and never logs or echoes it.

``GET /api/v0/nodes?limit=10000`` (state API, ``dashboard/modules/state/state_head.py``
``list_nodes`` -> ``state_api_utils.handle_list_api``/``do_reply``) answers
``{"result": bool, "msg": str, "data": {"result": {"total", "num_after_truncation",
"num_filtered", "result": [NodeState...], "partial_failure_warning"}}}``; each NodeState
(``ray/util/state/common.py``) carries ``node_id`` and ``state`` (``ALIVE``/``DEAD``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

ENV_RAY_AUTH = "WADDLEAI_RAY_AUTH_TOKEN"
ENV_RAY_AUTH_FILE = "RAY_AUTH_TOKEN_PATH"
DEFAULT_RAY_AUTH_FILE = "/etc/ray-auth/token"
_SERVE_PATH = "/api/serve/applications/"
_NODES_PATH = "/api/v0/nodes"
_NODES_LIMIT = 10000  # RAY_MAX_LIMIT_FROM_API_SERVER default; the server rejects more
_INVENTORY_ENTRYPOINT = "python3 -m shared.fleet.vllm_ray.inventory_job"
_PAYLOAD_PREFIX = "WAGPU_JSON:"
_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "STOPPED"})
_BODY_LIMIT = 200


class RayClientError(RuntimeError):
    """Ray REST failure carrying HTTP status and a truncated body; never the token."""

    def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
        """Store the message plus optional HTTP status and truncated body."""
        super().__init__(message)
        self.status = status
        self.body = body[:_BODY_LIMIT]


class RayServeClient(Protocol):
    """Operations the vLLM-on-Ray backend needs from the Ray head."""

    async def put_applications(self, config: dict[str, Any]) -> None:
        """PUT the full desired ``{"applications": [...]}`` set."""
        ...

    async def get_application(self, name: str) -> dict[str, Any]:
        """Return ``{"status", "message"}`` for an app; ``NOT_FOUND`` when absent."""
        ...

    async def delete_application(self, name: str, remaining: dict[str, Any]) -> None:
        """Remove ``name`` by PUTting the full config without it."""
        ...

    async def run_inventory(self) -> list[dict[str, Any]]:
        """Run the per-worker GPU inventory job; return one payload per worker."""
        ...

    async def list_alive_node_ids(self) -> list[str]:
        """Return the Ray node ids the head reports ALIVE (the trusted node list)."""
        ...


class HttpRayServeClient:
    """httpx implementation of :class:`RayServeClient` (bearer-token authenticated)."""

    def __init__(
        self,
        head_url: str,
        token: str | None,
        timeout_s: float = 10.0,
        *,
        poll_interval_s: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Validate the URL and token (fail closed) and store client settings."""
        if urlparse(head_url).scheme not in ("http", "https"):
            raise ValueError("head_url must use http or https")
        if token is None or not token.strip():
            raise RayClientError("ray auth token unavailable")
        self._base = head_url.rstrip("/")
        self._token = token.strip()
        self._timeout_s = timeout_s
        self._timeout = httpx.Timeout(timeout_s)
        self._poll_interval_s = poll_interval_s
        self._transport = transport
        self._head_url = self._base

    @classmethod
    def from_env(cls, head_url: str, **kwargs: Any) -> HttpRayServeClient:
        """Build a client with the token from env, else the mounted token file."""
        token = os.environ.get(ENV_RAY_AUTH)
        if not token or not token.strip():
            path = Path(os.environ.get(ENV_RAY_AUTH_FILE) or DEFAULT_RAY_AUTH_FILE)
            try:
                token = path.read_text()
            except OSError:
                token = None
        return cls(head_url, token, **kwargs)

    def __repr__(self) -> str:
        """Mask the token."""
        return f"HttpRayServeClient(head_url={self._head_url}, token=***)"

    @asynccontextmanager
    async def _http(self) -> AsyncIterator[httpx.AsyncClient]:
        """Yield an AsyncClient with the bearer header, timeout and optional transport."""
        async with httpx.AsyncClient(
            base_url=self._base,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=self._timeout,
            transport=self._transport,
        ) as http:
            yield http

    async def _request(
        self, http: httpx.AsyncClient, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        """Send one request; map transport errors and non-2xx to RayClientError."""
        logger.debug("ray request %s %s", method, path)
        try:
            resp = await http.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise RayClientError(f"ray {method} {path} timeout ({type(exc).__name__})") from None
        except httpx.HTTPError as exc:
            raise RayClientError(f"ray {method} {path} failed: {type(exc).__name__}") from None
        if not resp.is_success:
            body = resp.text.replace(self._token, "***")[:_BODY_LIMIT]
            raise RayClientError(
                f"ray {method} {path} returned {resp.status_code}: {body}",
                status=resp.status_code,
                body=body,
            )
        return resp

    async def put_applications(self, config: dict[str, Any]) -> None:
        """PUT the full desired application set."""
        async with self._http() as http:
            await self._request(http, "PUT", _SERVE_PATH, json=config)

    async def get_application(self, name: str) -> dict[str, Any]:
        """Return the app's ``status``/``message`` or ``NOT_FOUND``."""
        async with self._http() as http:
            resp = await self._request(http, "GET", _SERVE_PATH)
        apps = resp.json().get("applications") or {}
        entry = apps.get(name)
        if entry is None:
            return {"status": "NOT_FOUND", "message": ""}
        return {"status": entry.get("status", ""), "message": entry.get("message", "")}

    async def delete_application(self, name: str, remaining: dict[str, Any]) -> None:
        """Delete ``name`` by PUTting the config without it."""
        await self.put_applications(remaining)

    async def list_alive_node_ids(self) -> list[str]:
        """Return ALIVE node ids from the authenticated state API; fail closed if unusable.

        A failed reply, a malformed shape, a partial failure or a truncated list all raise
        :class:`RayClientError` -- an incomplete node list must never act as authoritative.
        """
        async with self._http() as http:
            resp = await self._request(http, "GET", _NODES_PATH, params={"limit": _NODES_LIMIT})
        try:
            body = resp.json()
            if body.get("result") is not True:
                raise ValueError("result flag false")
            page = body["data"]["result"]
            rows = page["result"]
            if not isinstance(rows, list):
                raise ValueError("rows not a list")
            ids = [str(r["node_id"]) for r in rows if r.get("state") == "ALIVE"]
            truncated = int(page.get("num_filtered", len(rows))) > len(rows)
            partial = bool(page.get("partial_failure_warning"))
        except (ValueError, KeyError, TypeError, AttributeError):
            raise RayClientError("ray nodes response malformed") from None
        if truncated or partial:
            raise RayClientError("ray nodes list truncated or partial")
        return ids

    async def run_inventory(self) -> list[dict[str, Any]]:
        """Submit the inventory job, poll to a terminal state, parse WAGPU_JSON log lines."""
        deadline = time.monotonic() + self._timeout_s * 6
        async with self._http() as http:
            resp = await self._request(
                http,
                "POST",
                "/api/jobs/",
                json={"entrypoint": _INVENTORY_ENTRYPOINT, "runtime_env": {}},
            )
            sub_id = resp.json().get("submission_id")
            if not sub_id:
                raise RayClientError("ray job submit response lacked submission_id")
            while True:
                info = (await self._request(http, "GET", f"/api/jobs/{sub_id}")).json()
                status = info.get("status", "")
                if status in _TERMINAL:
                    break
                if time.monotonic() >= deadline:
                    raise RayClientError(f"ray inventory job {sub_id} deadline exceeded")
                await asyncio.sleep(self._poll_interval_s)
            if status != "SUCCEEDED":
                raise RayClientError(f"ray inventory job {sub_id} ended {status}")
            logs = (await self._request(http, "GET", f"/api/jobs/{sub_id}/logs")).json()
        return _parse_payloads(logs.get("logs", ""))


def _parse_payloads(logs: str) -> list[dict[str, Any]]:
    """Extract JSON payloads from ``WAGPU_JSON:`` lines; malformed lines fail loudly."""
    out: list[dict[str, Any]] = []
    for line in logs.splitlines():
        if not line.startswith(_PAYLOAD_PREFIX):
            continue
        try:
            out.append(json.loads(line[len(_PAYLOAD_PREFIX) :]))
        except json.JSONDecodeError:
            raise RayClientError("ray inventory job emitted malformed WAGPU_JSON line") from None
    return out
