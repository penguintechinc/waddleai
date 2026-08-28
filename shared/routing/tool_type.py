"""Tool-type determination cascade orchestrator (spec §7.1, §7.2).

Wires cascade stages 0 -> 1 -> 2 cheapest-first: explicit signal, then
heuristic rules, then the classifier -- each stage is only consulted when the
prior one punts (returns None).
"""

from dataclasses import dataclass, field
from typing import Any

from shared.routing.classifier import Classification, ClassifierClient, classify
from shared.routing.heuristics import HeuristicRule, RequestSignals, evaluate_rules

_DEFAULT_FALLBACK_TOOL_TYPE = "general"


@dataclass(slots=True)
class ToolTypeDecision:
    """The resolved tool type plus provenance for the decision trace."""

    tool_type: str
    source: str  # "explicit" | "heuristic" | "classifier"
    rules_fired: list[str] = field(default_factory=list)
    classification: Classification | None = None


async def determine_tool_type(
    *,
    explicit: str | None = None,
    signals: RequestSignals,
    rules: list[HeuristicRule],
    prompt_text: str = "",
    classifier_prompt: str | None = None,
    classifier_client: ClassifierClient | None = None,
    classifier_model: str = "gemma4:e2b",
    valkey: Any = None,
) -> ToolTypeDecision:
    """Run the tool-type cascade, consulting each stage only when the prior punts.

    Args:
        explicit: Pre-computed stage-0 result (see aliases.explicit_tool_type),
            or None.
        signals: Cheap request signals for stage-1 heuristic matching.
        rules: The org's routing_rules_v2 rows (already loaded).
        prompt_text: The (already-summarized/truncated) request text, used
            only if stage 2 runs.
        classifier_prompt: Org-configured classifier_prompt (§7.3), used as
            the guard model's system prompt when stage 2 runs.
        classifier_client: The stage-2 classifier connector; when None,
            stage 2 is skipped and a safe default tool type is returned.
        classifier_model: Model to invoke for stage 2 (routing-classifier
            assignment).
        valkey: Optional cache client for the classifier's prefix-hash cache.

    Returns:
        ToolTypeDecision recording which stage resolved it.

    """
    if explicit:
        return ToolTypeDecision(tool_type=explicit, source="explicit")

    action = evaluate_rules(signals, rules)
    if action is not None:
        tool_type = action.get("tool_type")
        if tool_type:
            return ToolTypeDecision(
                tool_type=tool_type, source="heuristic", rules_fired=[str(action)]
            )

    if classifier_client is None:
        return ToolTypeDecision(tool_type=_DEFAULT_FALLBACK_TOOL_TYPE, source="classifier")

    classification = await classify(
        prompt_text,
        classifier_client,
        model=classifier_model,
        system_prompt=classifier_prompt,
        valkey=valkey,
    )
    return ToolTypeDecision(
        tool_type=classification.tool_type,
        source="classifier",
        classification=classification,
    )
