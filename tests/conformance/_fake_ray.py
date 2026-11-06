"""In-memory ``RayServeClient`` for conformance/backend tests (Plan B6; used by B7, B9)."""

from __future__ import annotations

from typing import Any


class FakeRayServeClient:
    """Implements the RayServeClient Protocol without HTTP.

    ``apps`` is replaced by each PUT; ``puts`` records every body; ``statuses`` scripts
    Serve status per app (missing -> NOT_FOUND); ``fail_next_put``/``fail_next_get`` raise once.
    """

    def __init__(self) -> None:
        """Start with no apps, no scripted statuses and no pending failures."""
        self.apps: dict[str, dict[str, Any]] = {}
        self.puts: list[dict[str, Any]] = []
        self.statuses: dict[str, dict[str, Any]] = {}
        self.inventory_payloads: list[dict[str, Any]] = []
        self.fail_next_put: Exception | None = None
        self.fail_next_get: Exception | None = None

    async def put_applications(self, config: dict[str, Any]) -> None:
        """Record the body and replace ``apps`` with the full desired set."""
        if self.fail_next_put is not None:
            exc, self.fail_next_put = self.fail_next_put, None
            raise exc
        self.puts.append(config)
        self.apps = {app["name"]: app for app in config.get("applications", [])}

    async def get_application(self, name: str) -> dict[str, Any]:
        """Return the scripted status, or NOT_FOUND."""
        if self.fail_next_get is not None:
            exc, self.fail_next_get = self.fail_next_get, None
            raise exc
        return self.statuses.get(name, {"status": "NOT_FOUND", "message": ""})

    async def delete_application(self, name: str, remaining: dict[str, Any]) -> None:
        """PUT ``remaining`` (the config without ``name``)."""
        await self.put_applications(remaining)

    async def run_inventory(self) -> list[dict[str, Any]]:
        """Return the scripted inventory payloads."""
        return list(self.inventory_payloads)
