"""Async Ollama API client."""

import json
from collections.abc import AsyncIterator

import httpx

from .types import (
    ChatRequest,
    ChatResponse,
    GenerateRequest,
    GenerateResponse,
    Message,
    ModelInfo,
    ToolCall,
)


class OllamaClient:
    """Async client for Ollama API."""

    def __init__(self, base_url: str = "http://localhost:11434", timeout: int = 120):
        """
        Initialize Ollama client.

        Args:
            base_url: Ollama API base URL
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        """Async context manager entry."""
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout),
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        self._ensure_client()
        return self._client  # type: ignore[return-value]

    def _ensure_client(self) -> None:
        """Raise if the HTTP client has not been initialized via context manager."""
        if self._client is None:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")

    async def generate(
        self,
        model: str,
        prompt: str,
        system: str | None = None,
        stream: bool = True,
        **kwargs,
    ) -> AsyncIterator[GenerateResponse]:
        """
        Generate completion from prompt.

        Args:
            model: Model name
            prompt: Prompt text
            system: System prompt
            stream: Whether to stream responses
            **kwargs: Additional generation parameters

        Yields:
            GenerateResponse objects
        """
        request = GenerateRequest(
            model=model,
            prompt=prompt,
            system=system,
            stream=stream,
            **kwargs,
        )

        async with self.client.stream(
            "POST",
            "/api/generate",
            json=self._to_dict(request),
        ) as response:
            response.raise_for_status()

            async for line in response.aiter_lines():
                if line.strip():
                    data = json.loads(line)
                    yield GenerateResponse(**data)

    async def chat(
        self,
        model: str,
        messages: list[Message],
        stream: bool = True,
        tools: list[dict] | None = None,
        **kwargs,
    ) -> AsyncIterator[ChatResponse]:
        """
        Chat with model using message history.

        Args:
            model: Model name
            messages: List of messages
            stream: Whether to stream responses
            tools: Optional tool definitions
            **kwargs: Additional chat parameters

        Yields:
            ChatResponse objects
        """
        request = ChatRequest(
            model=model,
            messages=messages,
            stream=stream,
            tools=tools,
            **kwargs,
        )

        async with self.client.stream(
            "POST",
            "/api/chat",
            json=self._to_dict(request),
        ) as response:
            response.raise_for_status()

            async for line in response.aiter_lines():
                if line.strip():
                    data = json.loads(line)
                    # Convert message dict to Message object
                    if "message" in data:
                        msg_data = data["message"]
                        # Parse tool_calls if present
                        tool_calls = None
                        if "tool_calls" in msg_data and msg_data["tool_calls"]:
                            tool_calls = [
                                ToolCall(function=tc.get("function", {}))
                                for tc in msg_data["tool_calls"]
                            ]
                        data["message"] = Message(
                            role=msg_data.get("role", "assistant"),
                            content=msg_data.get("content", ""),
                            images=msg_data.get("images"),
                            tool_calls=tool_calls,
                        )
                    # Filter to known ChatResponse fields to handle API changes
                    known_fields = {
                        "model", "created_at", "message", "done", "done_reason",
                        "total_duration", "load_duration", "prompt_eval_count",
                        "prompt_eval_duration", "eval_count", "eval_duration",
                    }
                    filtered_data = {k: v for k, v in data.items() if k in known_fields}
                    yield ChatResponse(**filtered_data)

    async def list_models(self) -> list[ModelInfo]:
        """
        List available models.

        Returns:
            List of ModelInfo objects
        """
        response = await self.client.get("/api/tags")
        response.raise_for_status()
        data = response.json()
        return [ModelInfo(**model) for model in data.get("models", [])]

    async def show_model(self, name: str) -> dict:
        """
        Get model information.

        Args:
            name: Model name

        Returns:
            Model info dictionary
        """
        response = await self.client.post(
            "/api/show",
            json={"name": name},
        )
        response.raise_for_status()
        return response.json()

    async def pull_model(self, name: str) -> AsyncIterator[dict]:
        """
        Pull a model from registry.

        Args:
            name: Model name

        Yields:
            Progress dictionaries
        """
        async with self.client.stream(
            "POST",
            "/api/pull",
            json={"name": name},
        ) as response:
            response.raise_for_status()

            async for line in response.aiter_lines():
                if line.strip():
                    yield json.loads(line)

    async def delete_model(self, name: str) -> bool:
        """
        Delete a model.

        Args:
            name: Model name

        Returns:
            True if successful
        """
        response = await self.client.delete(
            "/api/delete",
            json={"name": name},
        )
        response.raise_for_status()
        return True

    async def check_health(self) -> bool:
        """
        Check if Ollama server is healthy.

        Returns:
            True if server is responding
        """
        try:
            response = await self.client.get("/")
            return response.status_code == 200
        except Exception:
            return False

    @staticmethod
    def _to_dict(obj) -> dict:
        """Convert dataclass to dict, removing None values."""
        if hasattr(obj, "__dataclass_fields__"):
            result = {}
            for key, value in obj.__dict__.items():
                if value is not None:
                    if isinstance(value, list) and value and hasattr(value[0], "__dict__"):
                        result[key] = [
                            OllamaClient._to_dict(item) if hasattr(item, "__dataclass_fields__") else item.__dict__
                            for item in value
                        ]
                    elif hasattr(value, "__dataclass_fields__"):
                        result[key] = OllamaClient._to_dict(value)
                    else:
                        result[key] = value
            return result
        return obj.__dict__


# Convenience function
async def create_client(base_url: str = "http://localhost:11434", timeout: int = 120) -> OllamaClient:
    """
    Create and return an Ollama client.

    Args:
        base_url: Ollama API base URL
        timeout: Request timeout

    Returns:
        OllamaClient instance (use with async context manager)
    """
    return OllamaClient(base_url, timeout)
