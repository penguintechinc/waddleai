"""Organisational config client — pulls MCP servers, skills, and model config from the API server."""

import asyncio
import logging

import httpx

from penguincode_cli.client.auth import TokenManager

logger = logging.getLogger(__name__)

# Timeout for individual HTTP requests to the management API.
_DEFAULT_TIMEOUT = 10


class OrgConfigClient:
    """Pulls organisational MCP servers, skills, and model config from the management API server.

    Auth flow:
        1. If a valid JWT exists in ``TokenManager`` — use it.
        2. Otherwise, exchange ``shared_key`` for a JWT via ``/api/v1/provision``.
        3. Falls back gracefully if the server is unreachable.
    """

    def __init__(
        self,
        server_url: str,
        shared_key: str = "",
        token_path: str = "~/.penguincode/token",
    ):
        self.server_url = server_url.rstrip("/")
        self.shared_key = shared_key
        self.token_manager = TokenManager(token_path)

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self) -> bool:
        """Authenticate with the shared key to obtain a JWT.

        Returns ``True`` if a valid token is now available.
        """
        # Already have a valid token?
        if self.token_manager.get_token():
            return True

        if not self.shared_key:
            return False

        try:
            async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
                resp = await client.post(
                    f"{self.server_url}/api/v1/provision",
                    json={"license_key": self.shared_key},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    self.token_manager.store_token(
                        access_token=data["access_token"],
                        refresh_token=data.get("refresh_token", ""),
                        expires_in=data.get("expires_in", 3600),
                    )
                    return True
                logger.warning("Org auth failed: HTTP %s", resp.status_code)
        except Exception as exc:
            logger.debug("Org auth unavailable: %s", exc)

        return False

    # ------------------------------------------------------------------
    # Fetch helpers
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        token = self.token_manager.get_token()
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    async def _get(self, path: str) -> list[dict] | None:
        """GET a JSON list from the server, or ``None`` on failure."""
        try:
            async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
                resp = await client.get(
                    f"{self.server_url}{path}",
                    headers=self._auth_headers(),
                )
                if resp.status_code == 200:
                    return resp.json()
                logger.warning("Org config GET %s: HTTP %s", path, resp.status_code)
        except Exception as exc:
            logger.debug("Org config GET %s failed: %s", path, exc)
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_mcp_servers(self) -> list[dict]:
        """Fetch MCP server configs from the management API."""
        return await self._get("/api/v1/mcp-servers") or []

    async def fetch_skills(self) -> list[dict]:
        """Fetch skill definitions from the management API."""
        return await self._get("/api/v1/skills") or []

    async def fetch_models(self) -> list[dict]:
        """Fetch model role assignments from the management API."""
        return await self._get("/api/v1/models") or []

    async def fetch_all(self) -> dict:
        """Fetch all org config in parallel.

        Returns::

            {"mcp_servers": [...], "skills": [...], "models": [...]}
        """
        mcp_servers, skills, models = await asyncio.gather(
            self.fetch_mcp_servers(),
            self.fetch_skills(),
            self.fetch_models(),
            return_exceptions=True,
        )

        return {
            "mcp_servers": mcp_servers if isinstance(mcp_servers, list) else [],
            "skills": skills if isinstance(skills, list) else [],
            "models": models if isinstance(models, list) else [],
        }
