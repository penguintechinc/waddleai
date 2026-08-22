"""Cascade stage 2 -- the routing classifier (spec §7.1, §7.2, §14.4).

Only consulted when stage-1 heuristics punt. A guard model returns structured
JSON whose primary output is ``tool_type`` plus ``{complexity, domain,
needs_reasoning}``, cached in Valkey by prefix hash. Model per §2.3:
``gemma4:e2b`` default (Apache-2.0, no dual-default alternative required),
resolved via the ``routing-classifier`` assignment row. Stubbed in the unit
test tier (see StubClassifierClient); a ``@pytest.mark.gpu`` nightly test
exercises the real model.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_CACHE_PREFIX = "waddleai:route:cls"
_DEFAULT_CACHE_TTL = 3600
_DEFAULT_CLASSIFIER_MODEL = "gemma4:e2b"

_SAFE_DEFAULT_TOOL_TYPE = "general"
_SAFE_DEFAULT_COMPLEXITY = 1


@dataclass(slots=True)
class Classification:
    """Structured classifier output."""

    tool_type: str
    complexity: int
    domain: str = "general"
    needs_reasoning: bool = False


class ClassifierClient(Protocol):
    """Minimal interface any guard-model connector must satisfy."""

    async def complete(self, prompt: str, model: str, system_prompt: str | None = None) -> str:
        """Return the raw (expected-JSON) completion text for prompt."""
        ...


class StubClassifierClient:
    """Deterministic stub classifier for the unit test tier (spec §14.4).

    Returns a fixed structured payload (or a caller-supplied override)
    without any network/model call, so unit tests never depend on a live
    fleet backend.
    """

    def __init__(self, fixed_response: str | None = None) -> None:
        """Initialize with an optional fixed raw response string."""
        self.fixed_response = fixed_response or json.dumps(
            {"tool_type": "chat", "complexity": 2, "domain": "general", "needs_reasoning": False}
        )
        self.call_count = 0

    async def complete(self, prompt: str, model: str, system_prompt: str | None = None) -> str:
        """Return the fixed response, tracking how many times it was called."""
        self.call_count += 1
        return self.fixed_response


def _prefix_hash(prompt: str) -> str:
    """SHA-256 of the prompt prefix used as the Valkey cache key component."""
    prefix = prompt[:512]
    return hashlib.sha256(prefix.encode("utf-8")).hexdigest()


def _cache_key(prompt: str) -> str:
    """Build the Valkey cache key for a classifier prompt."""
    return f"{_CACHE_PREFIX}:{_prefix_hash(prompt)}"


def _parse_classification(raw: str) -> Classification:
    """Parse structured JSON classifier output; malformed output raises ValueError."""
    data = json.loads(raw)
    tool_type = data["tool_type"]
    if not isinstance(tool_type, str) or not tool_type:
        raise ValueError("empty tool_type")
    complexity = int(data.get("complexity", _SAFE_DEFAULT_COMPLEXITY))
    complexity = max(1, min(5, complexity))
    return Classification(
        tool_type=tool_type,
        complexity=complexity,
        domain=str(data.get("domain", "general")),
        needs_reasoning=bool(data.get("needs_reasoning", False)),
    )


async def classify(
    prompt: str,
    client: ClassifierClient,
    model: str = _DEFAULT_CLASSIFIER_MODEL,
    system_prompt: str | None = None,
    valkey: Any = None,
    cache_ttl: int = _DEFAULT_CACHE_TTL,
) -> Classification:
    """Classify a prompt's tool type + complexity, with prefix-hash caching.

    Args:
        prompt: The (already-summarized/truncated) request text to classify.
        client: The guard-model connector (StubClassifierClient in unit tests).
        model: The classifier model to invoke, normally resolved from the
            routing-classifier assignment row.
        system_prompt: Optional org-configured classifier_prompt (§7.3).
        valkey: Optional cache client.
        cache_ttl: Cache entry TTL in seconds.

    Returns:
        The Classification. Malformed/non-JSON model output degrades to a
        safe default (tool_type="general", low complexity) rather than
        raising -- a classifier failure must never break the request.

    """
    cache_key = _cache_key(prompt)
    cached = await _cache_get(valkey, cache_key)
    if cached is not None:
        return cached

    try:
        raw = await client.complete(prompt, model, system_prompt=system_prompt)
        result = _parse_classification(raw)
    except Exception as exc:
        logger.warning("classifier: malformed output, using safe default: %s", exc)
        result = Classification(
            tool_type=_SAFE_DEFAULT_TOOL_TYPE, complexity=_SAFE_DEFAULT_COMPLEXITY
        )

    await _cache_set(valkey, cache_key, result, cache_ttl)
    return result


async def _cache_get(valkey: Any, key: str) -> Classification | None:
    """Read-through cache lookup; any failure is treated as a miss."""
    if valkey is None:
        return None
    try:
        raw = await valkey.get(key)
    except Exception as exc:  # pragma: no cover - defensive, Valkey I/O failure
        logger.warning("classifier: cache read failed: %s", exc)
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return Classification(**data)
    except (ValueError, TypeError, KeyError):
        return None


async def _cache_set(valkey: Any, key: str, result: Classification, ttl: int) -> None:
    """Write-through cache store."""
    if valkey is None:
        return
    try:
        payload = json.dumps(
            {
                "tool_type": result.tool_type,
                "complexity": result.complexity,
                "domain": result.domain,
                "needs_reasoning": result.needs_reasoning,
            }
        )
        await valkey.set(key, payload, ex=ttl)
    except Exception as exc:  # pragma: no cover - defensive, Valkey I/O failure
        logger.warning("classifier: cache write failed: %s", exc)
