"""Agent implementations for PenguinCode."""

from .base import TOOL_DEFINITIONS, AgentConfig, AgentResult, BaseAgent, Permission
from .chat import ChatAgent
from .debugger import DebuggerAgent
from .docs import DocsAgent
from .executor import ExecutorAgent
from .explorer import ExplorerAgent
from .planner import Plan, PlannerAgent, PlanStep
from .refactor import RefactorAgent
from .researcher import ResearcherAgent
from .reviewer import ReviewerAgent
from .tester import TesterAgent

__all__ = [
    # Base classes
    "BaseAgent",
    "AgentConfig",
    "AgentResult",
    "Permission",
    "TOOL_DEFINITIONS",
    # Main orchestrator
    "ChatAgent",
    # Specialized agents
    "DebuggerAgent",
    "DocsAgent",
    "ExecutorAgent",
    "ExplorerAgent",
    "PlannerAgent",
    "Plan",
    "PlanStep",
    "RefactorAgent",
    "ResearcherAgent",
    "ReviewerAgent",
    "TesterAgent",
]
