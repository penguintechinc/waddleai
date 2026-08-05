"""Type definitions for Ollama API."""

from dataclasses import dataclass
from typing import Any


@dataclass
class ToolCall:
    """Tool call from assistant message."""

    function: dict[str, Any]


@dataclass
class Message:
    """Chat message."""

    role: str  # "system", "user", "assistant"
    content: str
    images: list[str] | None = None  # For vision models
    tool_calls: list[ToolCall] | None = None  # Tool calls from assistant

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        """Create Message from a dictionary."""
        tool_calls = None
        if data.get("tool_calls"):
            tool_calls = [ToolCall(function=tc.get("function", {})) for tc in data["tool_calls"]]
        return cls(
            role=data.get("role", "user"),
            content=data.get("content", ""),
            images=data.get("images"),
            tool_calls=tool_calls,
        )


@dataclass
class GenerateRequest:
    """Generate API request parameters."""

    model: str
    prompt: str
    system: str | None = None
    template: str | None = None
    context: list[int] | None = None
    stream: bool = True
    raw: bool = False
    format: str | None = None  # "json" for JSON mode
    options: dict[str, Any] | None = None
    keep_alive: str | None = None


@dataclass
class GenerateResponse:
    """Generate API response."""

    model: str
    created_at: str
    response: str
    done: bool
    context: list[int] | None = None
    total_duration: int | None = None
    load_duration: int | None = None
    prompt_eval_count: int | None = None
    prompt_eval_duration: int | None = None
    eval_count: int | None = None
    eval_duration: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GenerateResponse":
        """Create GenerateResponse from a dictionary."""
        return cls(
            model=data["model"],
            created_at=data["created_at"],
            response=data["response"],
            done=data["done"],
            context=data.get("context"),
            total_duration=data.get("total_duration"),
            load_duration=data.get("load_duration"),
            prompt_eval_count=data.get("prompt_eval_count"),
            prompt_eval_duration=data.get("prompt_eval_duration"),
            eval_count=data.get("eval_count"),
            eval_duration=data.get("eval_duration"),
        )


@dataclass
class ChatRequest:
    """Chat API request parameters."""

    model: str
    messages: list[Message]
    stream: bool = True
    format: str | None = None  # "json" for JSON mode
    options: dict[str, Any] | None = None
    keep_alive: str | None = None
    tools: list[dict[str, Any]] | None = None


@dataclass
class ChatResponse:
    """Chat API response."""

    model: str
    created_at: str
    message: Message
    done: bool
    done_reason: str | None = None
    total_duration: int | None = None
    load_duration: int | None = None
    prompt_eval_count: int | None = None
    prompt_eval_duration: int | None = None
    eval_count: int | None = None
    eval_duration: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChatResponse":
        """Create ChatResponse from a dictionary."""
        msg_data = data.get("message", {})
        message = Message.from_dict(msg_data) if isinstance(msg_data, dict) else msg_data
        return cls(
            model=data["model"],
            created_at=data["created_at"],
            message=message,
            done=data["done"],
            done_reason=data.get("done_reason"),
            total_duration=data.get("total_duration"),
            load_duration=data.get("load_duration"),
            prompt_eval_count=data.get("prompt_eval_count"),
            prompt_eval_duration=data.get("prompt_eval_duration"),
            eval_count=data.get("eval_count"),
            eval_duration=data.get("eval_duration"),
        )


@dataclass
class ModelInfo:
    """Model information."""

    name: str
    modified_at: str
    size: int
    digest: str
    details: dict[str, Any] | None = None


@dataclass
class UsageStats:
    """Token usage statistics from a response."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    prompt_eval_duration_ms: float = 0.0
    eval_duration_ms: float = 0.0
    total_duration_ms: float = 0.0

    @classmethod
    def from_response(cls, response: GenerateResponse) -> "UsageStats":
        """Create usage stats from a generate response."""
        return cls(
            prompt_tokens=response.prompt_eval_count or 0,
            completion_tokens=response.eval_count or 0,
            total_tokens=(response.prompt_eval_count or 0) + (response.eval_count or 0),
            prompt_eval_duration_ms=(response.prompt_eval_duration or 0) / 1_000_000,
            eval_duration_ms=(response.eval_duration or 0) / 1_000_000,
            total_duration_ms=(response.total_duration or 0) / 1_000_000,
        )

    @classmethod
    def from_chat_response(cls, response: ChatResponse) -> "UsageStats":
        """Create usage stats from a chat response."""
        return cls(
            prompt_tokens=response.prompt_eval_count or 0,
            completion_tokens=response.eval_count or 0,
            total_tokens=(response.prompt_eval_count or 0) + (response.eval_count or 0),
            prompt_eval_duration_ms=(response.prompt_eval_duration or 0) / 1_000_000,
            eval_duration_ms=(response.eval_duration or 0) / 1_000_000,
            total_duration_ms=(response.total_duration or 0) / 1_000_000,
        )
