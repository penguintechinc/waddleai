"""Base tool interface for PenguinCode."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolResult:
    """Result from a tool execution."""

    success: bool
    data: Any
    error: str | None = None
    metadata: dict[str, Any] | None = None


class BaseTool(ABC):
    """Abstract base class for all tools."""

    def __init__(self, name: str, description: str):
        """
        Initialize tool.

        Args:
            name: Tool name
            description: Tool description
        """
        self.name = name
        self.description = description

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """
        Execute the tool.

        Args:
            **kwargs: Tool-specific arguments

        Returns:
            ToolResult with execution outcome
        """
        pass

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"
