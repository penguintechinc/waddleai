"""Tests for ollama module - OllamaClient, Message, GenerateResponse types."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

try:
    from penguincode_cli.ollama.client import OllamaClient
    from penguincode_cli.ollama.types import ChatResponse, GenerateResponse, Message, ModelInfo
except ImportError:
    pytest.skip("Ollama module not available", allow_module_level=True)


@pytest.fixture
def mock_httpx_client():
    """Mock httpx.AsyncClient."""
    client = AsyncMock(spec=httpx.AsyncClient)
    return client


@pytest.fixture
async def ollama_client():
    """Create OllamaClient instance."""
    async with OllamaClient(base_url="http://test:11434") as client:
        yield client


def test_message_initialization():
    """Test Message dataclass initialization."""
    msg = Message(role="user", content="Hello")
    assert msg.role == "user"
    assert msg.content == "Hello"
    assert msg.images is None


def test_message_with_images():
    """Test Message with images."""
    msg = Message(role="user", content="Describe this", images=["image1.jpg"])
    assert msg.images == ["image1.jpg"]


def test_message_from_dict():
    """Test Message.from_dict factory method."""
    data = {"role": "assistant", "content": "Response", "images": None}
    msg = Message.from_dict(data)
    assert msg.role == "assistant"
    assert msg.content == "Response"


def test_generate_response_initialization():
    """Test GenerateResponse initialization."""
    resp = GenerateResponse(
        model="llama3.2:latest",
        created_at="2024-01-01T00:00:00Z",
        response="Test response",
        done=True,
        eval_count=10,
    )
    assert resp.model == "llama3.2:latest"
    assert resp.response == "Test response"
    assert resp.done is True
    assert resp.eval_count == 10


def test_generate_response_from_dict():
    """Test GenerateResponse.from_dict factory method."""
    data = {
        "model": "test:latest",
        "created_at": "2024-01-01T00:00:00Z",
        "response": "Generated",
        "done": False,
        "eval_count": 5,
        "total_duration": 1000,
    }
    resp = GenerateResponse.from_dict(data)
    assert resp.model == "test:latest"
    assert resp.response == "Generated"
    assert resp.done is False


def test_chat_response_initialization():
    """Test ChatResponse initialization."""
    msg = Message(role="assistant", content="Chat response")
    resp = ChatResponse(
        model="llama3.2:latest",
        created_at="2024-01-01T00:00:00Z",
        message=msg,
        done=True,
    )
    assert resp.model == "llama3.2:latest"
    assert resp.message.content == "Chat response"
    assert resp.done is True


def test_chat_response_from_dict():
    """Test ChatResponse.from_dict factory method."""
    data = {
        "model": "test:latest",
        "created_at": "2024-01-01T00:00:00Z",
        "message": {"role": "assistant", "content": "Response"},
        "done": True,
        "eval_count": 10,
    }
    resp = ChatResponse.from_dict(data)
    assert resp.model == "test:latest"
    assert resp.message.role == "assistant"


def test_model_info_initialization():
    """Test ModelInfo initialization."""
    info = ModelInfo(
        name="llama3.2:latest",
        modified_at="2024-01-01T00:00:00Z",
        size=1024000,
        digest="abc123",
        details={"parameter_size": "7B"},
    )
    assert info.name == "llama3.2:latest"
    assert info.size == 1024000
    assert info.details["parameter_size"] == "7B"


def test_ollama_client_initialization():
    """Test OllamaClient initialization."""
    client = OllamaClient(base_url="http://custom:11434", timeout=60.0)
    assert client.base_url == "http://custom:11434"
    assert client.timeout == 60.0


@pytest.mark.asyncio
async def test_ollama_client_context_manager():
    """Test OllamaClient context manager."""
    async with OllamaClient() as client:
        assert client._client is not None


@pytest.mark.asyncio
async def test_ollama_client_ensure_client_raises():
    """Test _ensure_client raises when not initialized."""
    client = OllamaClient()
    with pytest.raises(RuntimeError, match="Client not initialized"):
        client._ensure_client()


@pytest.mark.asyncio
async def test_generate_streaming(ollama_client):
    """Test generate method with streaming."""
    # Mock the streaming response
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_lines():
        yield '{"model":"test","created_at":"2024-01-01T00:00:00Z","response":"Test","done":false}'
        yield '{"model":"test","created_at":"2024-01-01T00:00:00Z","response":" response","done":true}'

    mock_response.aiter_lines = mock_aiter_lines

    with patch.object(ollama_client._client, "stream") as mock_stream:
        mock_stream.return_value.__aenter__.return_value = mock_response

        responses = []
        async for resp in ollama_client.generate("test", "Hello"):
            responses.append(resp)

        assert len(responses) == 2
        assert responses[0].response == "Test"
        assert responses[1].done is True


@pytest.mark.asyncio
async def test_chat_method(ollama_client):
    """Test chat method."""
    messages = [Message(role="user", content="Hello")]

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()

    async def mock_aiter_lines():
        yield (
            '{"model":"test","created_at":"2024-01-01T00:00:00Z",'
            '"message":{"role":"assistant","content":"Hi"},"done":true}'
        )

    mock_response.aiter_lines = mock_aiter_lines

    with patch.object(ollama_client._client, "stream") as mock_stream:
        mock_stream.return_value.__aenter__.return_value = mock_response

        responses = []
        async for resp in ollama_client.chat("test", messages):
            responses.append(resp)

        assert len(responses) == 1
        assert responses[0].message.content == "Hi"


@pytest.mark.asyncio
async def test_list_models(ollama_client):
    """Test list_models method."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "models": [
            {
                "name": "llama3.2:latest",
                "modified_at": "2024-01-01T00:00:00Z",
                "size": 1024000,
                "digest": "abc123",
                "details": {},
            }
        ]
    }

    with patch.object(ollama_client._client, "get", return_value=mock_response):
        models = await ollama_client.list_models()
        assert len(models) == 1
        assert models[0].name == "llama3.2:latest"


@pytest.mark.asyncio
async def test_check_health_success(ollama_client):
    """Test check_health when server is healthy."""
    mock_response = MagicMock()
    mock_response.status_code = 200

    with patch.object(ollama_client._client, "get", return_value=mock_response):
        is_healthy = await ollama_client.check_health()
        assert is_healthy is True


@pytest.mark.asyncio
async def test_check_health_failure(ollama_client):
    """Test check_health when server is unhealthy."""
    with patch.object(ollama_client._client, "get", side_effect=Exception("Connection error")):
        is_healthy = await ollama_client.check_health()
        assert is_healthy is False
