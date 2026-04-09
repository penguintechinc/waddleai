"""
Routing Agent — orchestrates complexity classification and model selection.

Combines the deterministic :class:`MatrixFactorizationClassifier` with the
database-backed :class:`RoutingMatrix` to produce a single
:class:`RouteDecision` for every inbound prompt.
"""

import logging
from typing import Optional

from shared.agents.mf_classifier import MatrixFactorizationClassifier
from shared.agents.routing_matrix import RouteDecision, RoutingMatrix

logger = logging.getLogger(__name__)

# Hard-coded fallback used when both the matrix and classifier cannot
# produce a result.
_FALLBACK_MODEL = "llama3.1:8b"


class RoutingAgent:
    """Evaluate a prompt and select the best model.

    Args:
        db: A penguin-dal (PyDAL-compatible) database connection used by
            the :class:`RoutingMatrix`.
        embedding_manager: An :class:`EmbeddingManager` instance.  Currently
            reserved for future semantic-routing features; not used in the
            deterministic path.
    """

    def __init__(self, db, embedding_manager) -> None:  # type: ignore[type-arg]
        self._matrix = RoutingMatrix(db)
        self._classifier = MatrixFactorizationClassifier()
        self._embedding_manager = embedding_manager

    async def evaluate(
        self,
        prompt: str,
        tool_type: str,
        region: str = "NA",
    ) -> RouteDecision:
        """Classify the prompt and look up the appropriate model.

        Args:
            prompt: The raw user prompt text.
            tool_type: Declared tool / language type (e.g. ``"python"``).
            region: Deployment region code (e.g. ``"NA"``, ``"EU"``).

        Returns:
            A :class:`RouteDecision` with the selected model, complexity
            label, and supporting metadata.
        """
        # 1. Classify complexity
        classification = self._classifier.score_detailed(prompt, tool_type)
        complexity = classification.complexity

        # 2. Look up model in routing matrix
        model: Optional[str] = self._matrix.lookup(tool_type, complexity, region)

        # 3. Determine confidence and reasoning
        if model is not None:
            confidence = 0.85 + (classification.raw_score * 0.15)
            reasoning = (
                f"Exact matrix match: tool_type={tool_type}, "
                f"complexity={complexity}, region={region}. "
                f"Classifier raw_score={classification.raw_score:.4f}."
            )
        else:
            # Fall back to wildcard or default
            model = self._matrix.lookup_with_default(tool_type, complexity, region)
            if model != _FALLBACK_MODEL:
                confidence = 0.60 + (classification.raw_score * 0.10)
                reasoning = (
                    f"Wildcard matrix match for complexity={complexity}, "
                    f"region={region}. "
                    f"Classifier raw_score={classification.raw_score:.4f}."
                )
            else:
                confidence = 0.40
                reasoning = (
                    f"No matrix entry found; using fallback model "
                    f"{_FALLBACK_MODEL}. "
                    f"Classifier raw_score={classification.raw_score:.4f}."
                )

        target_type = self._infer_target_type(tool_type)

        return RouteDecision(
            model=model,
            complexity=complexity,
            target_type=target_type,
            confidence=round(min(confidence, 1.0), 4),
            reasoning=reasoning,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _infer_target_type(tool_type: str) -> str:
        """Map tool_type to a coarse target category.

        Returns one of ``"code"``, ``"ops"``, ``"data"``, or ``"general"``.
        """
        code_tools = frozenset(
            {
                "python",
                "javascript",
                "typescript",
                "go",
                "rust",
                "java",
                "cpp",
                "code_review",
                "debug",
                "test_write",
                "refactor",
            }
        )
        ops_tools = frozenset({"bash", "devops", "file_edit"})
        data_tools = frozenset({"sql", "data_analysis"})

        lower = tool_type.lower()
        if lower in code_tools:
            return "code"
        if lower in ops_tools:
            return "ops"
        if lower in data_tools:
            return "data"
        return "general"
