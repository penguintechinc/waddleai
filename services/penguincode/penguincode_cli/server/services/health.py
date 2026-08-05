"""Health check service implementation."""

import grpc
from penguincode_cli.config.settings import Settings
from penguincode_cli.proto import HealthCheckRequest, HealthCheckResponse, HealthServiceServicer


class HealthServiceImpl(HealthServiceServicer):
    """Health check service for monitoring server status."""

    VERSION = "0.1.0"

    def __init__(self, settings: Settings):
        self.settings = settings
        self._chat_service = None  # Will be set by server

    def set_chat_service(self, chat_service) -> None:
        """Set reference to chat service for session count."""
        self._chat_service = chat_service

    async def Check(
        self,
        request: HealthCheckRequest,
        context: grpc.aio.ServicerContext,
    ) -> HealthCheckResponse:
        """Check server health status."""
        # Check Ollama connection
        ollama_connected = False
        try:
            import httpx

            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.settings.ollama.api_url}/api/tags",
                    timeout=5.0,
                )
                ollama_connected = response.status_code == 200
        except Exception:
            pass

        # Get active session count
        active_sessions = 0
        if self._chat_service:
            active_sessions = len(self._chat_service.sessions)

        return HealthCheckResponse(
            healthy=True,
            version=self.VERSION,
            ollama_connected=ollama_connected,
            active_sessions=active_sessions,
        )
