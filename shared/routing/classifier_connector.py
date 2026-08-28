"""Real stage-2 classifier connector (spec §7.2, §2.3): ``gemma4:e2b``.

Adapts an ``LLMConnectionManager`` (already built for provider dispatch,
``shared.utils.llm_connectors``) to the ``shared.routing.classifier.ClassifierClient``
protocol so ``RoutingEngine`` can run real stage-2 classification instead of
degrading to the safe ``"general"`` fallback with ``classifier_client=None``.

Model: ``gemma4:e2b`` (Apache-2.0, no dual-default alternative required per
§2.3) -- note valid Gemma 4 tags are ``e2b``/``e4b``/``12b``/``26b``/``31b``;
there is no ``2b`` tag and ``gemma4:2b`` is unpullable. This is the same
default as ``shared.routing.classifier._DEFAULT_CLASSIFIER_MODEL`` and the
``routing-classifier`` internal-function assignment row seeded by migration
010. It is distinct from the security-audit assignment's ShieldGemma model
(``shieldgemma:2b``, already wired via ``SECURITY_AUDITOR_MODEL`` in
``ContentFilter`` for prompt/response auditing) -- ShieldGemma is a safety
classifier, not the routing tool-type/complexity classifier.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Baked-in default instruction for when no org classifier_prompt is
# configured (routing_policies.classifier_prompt, spec §7.3). RoutingEngine
# currently resolves the org policy after running the tool-type cascade, so
# a per-org prompt cannot yet reach this connector for the very first call;
# this default keeps stage 2 genuinely functional (real structured JSON
# output) rather than a wired-but-useless connector until that ordering is
# revisited.
_DEFAULT_SYSTEM_PROMPT = (
    "You are a routing classifier. Read the user's request and respond with "
    "ONLY a single-line JSON object, no prose, no markdown fences, matching "
    'exactly this shape: {"tool_type": "<short-snake_case-tag>", '
    '"complexity": <integer 1-5>, "domain": "<short-snake_case-tag>", '
    '"needs_reasoning": <true|false>}. "complexity" is 1 for a trivial '
    "request and 5 for one requiring deep multi-step reasoning."
)

_MAX_TOKENS = 200
_TEMPERATURE = 0.0


class LLMConnectorClassifierClient:
    """Routes stage-2 classification prompts through a real LLM connector.

    Satisfies ``shared.routing.classifier.ClassifierClient``:
    ``async complete(prompt, model, system_prompt=None) -> str``.
    """

    def __init__(self, llm_manager: Any, fallback_provider: str = "ollama") -> None:
        """Initialize the adapter.

        Args:
            llm_manager: LLMConnectionManager exposing ``.connectors`` (name
                -> LLMConnector), the same instance the proxy already builds
                for provider dispatch.
            fallback_provider: Connector name to use when no connector's
                ``model_list`` explicitly advertises the classifier model --
                ``gemma4:e2b`` is Ollama-served by convention (matches the
                retired ``LLMRequestRouter._call_routing_llm``'s selection
                rule).

        """
        self.llm_manager = llm_manager
        self.fallback_provider = fallback_provider

    async def complete(self, prompt: str, model: str, system_prompt: str | None = None) -> str:
        """Call the classifier model and return its raw completion text.

        Never raises for a missing/unhealthy connector -- returns a
        non-JSON sentinel string instead, which ``shared.routing.classifier.classify()``
        already treats as malformed output and degrades to the safe default
        (tool_type="general"). A classifier failure must never break routing.
        """
        connector = self._pick_connector(model)
        if connector is None:
            logger.warning(
                "LLMConnectorClassifierClient: no connector available for model %s", model
            )
            return ""

        messages = [
            {"role": "system", "content": system_prompt or _DEFAULT_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        try:
            response_text, _usage = await connector.chat_completion(
                messages=messages,
                model=model,
                max_tokens=_MAX_TOKENS,
                temperature=_TEMPERATURE,
            )
            return response_text
        except Exception as exc:  # pragma: no cover - defensive, provider I/O failure
            logger.warning("LLMConnectorClassifierClient: classifier call failed: %s", exc)
            return ""

    def _pick_connector(self, model: str) -> Any | None:
        """Find a connector advertising ``model``, else fall back by provider name."""
        connectors = getattr(self.llm_manager, "connectors", {}) or {}
        for name, connector in connectors.items():
            model_list = getattr(connector, "model_list", None) or []
            if model in model_list or name == self.fallback_provider:
                return connector
        return None
