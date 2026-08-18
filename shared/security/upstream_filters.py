"""Pre-provider upstream query filters (§8.7).

Strips or transforms sensitive data from requests *before* they leave for an
upstream provider -- e.g. a hospital pre-filtering PHI before anything
reaches Anthropic. Detection reuses tiers 1-3 (regex + custom + NER); the
transform is a different *action* at the dispatch boundary, not new
scanning cost. Destination-aware via `applies_to: commercial | all` -- the
default protects only commercial destinations, so local fleet models (which
never leave the deployment) can still receive raw content. Two modes:
`redact` (irreversible masking) or `pseudonymize` (reversible placeholders,
map lives in Valkey for the request lifetime only; the response is
de-pseudonymized before returning to the client).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Compliance presets expand to the BUILTIN_PATTERNS names (content_filter.py)
# they cover -- reusing the same tier-1 pattern registry rather than
# inventing a parallel category taxonomy.
PRESETS: dict[str, set[str]] = {
    "hipaa": {
        "ssn",
        "ssn_unformatted",
        "medicare_id_us",
        "date_of_birth",
        "email",
        "phone_us",
        "phone_international",
        "national_id_uk",
        "drivers_license_us",
        "passport_us",
        "passport_generic",
    },
    "pci-dss": {"credit_card", "credit_card_amex", "routing_number", "iban"},
    "pii-basic": {"email", "phone_us", "ssn", "ip_address_public"},
}

_PSEUDO_KEY_PREFIX = "waddleai:pseudo:"
_PSEUDO_TTL_SECONDS = 600  # request-lifetime bound; cleanup() removes it explicitly on completion


def expand_preset(name: str) -> set[str]:
    """Return the builtin-pattern category set for a named compliance preset."""
    return set(PRESETS.get(name, ()))


@dataclass(slots=True)
class UpstreamFilterResult:
    """Result of one `UpstreamFilter.apply()` call."""

    text: str
    mapping_id: str | None = None
    counts: dict[str, int] = field(default_factory=dict)


class UpstreamFilter:
    """Pre-provider redact/pseudonymize filter, reusing tiers 1-3 detection."""

    def __init__(self, content_filter: Any, valkey: Any) -> None:
        """Wire the shared ContentFilter (tiers 1-3) and a Valkey client for the pseudonym map."""
        self.content_filter = content_filter
        self.valkey = valkey

    async def apply(
        self,
        text: str,
        resolved: Any,
        destination_kind: str,
        ctx: Any = None,
    ) -> UpstreamFilterResult:
        """Apply the resolved policy's upstream filter, if any, for this destination.

        `destination_kind` is "commercial" or "local". No-op (passthrough)
        when upstream filtering is unconfigured or `applies_to` excludes
        this destination.
        """
        config = getattr(resolved, "upstream_filters", None) or {}
        if not config:
            return UpstreamFilterResult(text=text)

        applies_to = config.get("applies_to", "commercial")
        if applies_to == "commercial" and destination_kind != "commercial":
            return UpstreamFilterResult(text=text)

        org_id = getattr(ctx, "org_id", None) if ctx is not None else None
        violations = await self._detect(text, org_id)

        categories = set(config.get("categories") or [])
        if categories:
            violations = [v for v in violations if v.rule_name in categories]

        counts: dict[str, int] = {}
        for v in violations:
            counts[v.rule_name] = counts.get(v.rule_name, 0) + 1

        if not violations:
            return UpstreamFilterResult(text=text, counts=counts)

        mode = config.get("mode", "redact")
        if mode == "pseudonymize":
            transformed, mapping = self._pseudonymize(text, violations)
            mapping_id = str(uuid.uuid4())
            await self._store_mapping(mapping_id, mapping)
            return UpstreamFilterResult(text=transformed, mapping_id=mapping_id, counts=counts)

        # redact: irreversible, reuses ContentFilter's own redaction logic
        _action, filtered = self.content_filter._determine_action(text, violations)
        return UpstreamFilterResult(text=filtered, mapping_id=None, counts=counts)

    async def _detect(self, text: str, org_id: Any) -> list[Any]:
        """Tiers 1-3 detection -- no tier-4 guard call (§8.7: reuses existing scanning)."""
        violations: list[Any] = []
        violations.extend(await self.content_filter._run_builtin_patterns(text, "input", org_id))
        violations.extend(await self.content_filter._run_custom_rules(text, "input", org_id))
        violations.extend(await self.content_filter._run_ner_patterns(text, "input", org_id))
        return violations

    @staticmethod
    def _pseudonymize(text: str, violations: list[Any]) -> tuple[str, dict[str, str]]:
        """Replace each violation's matched text with a reversible placeholder token."""
        mapping: dict[str, str] = {}
        result = text
        for i, v in enumerate(violations):
            original = getattr(v, "full_matched_text", "") or getattr(v, "matched_text", "")
            if not original or original not in result:
                continue
            placeholder = f"[PSEUDO_{v.rule_name.upper()}_{i}]"
            mapping[placeholder] = original
            result = result.replace(original, placeholder)
        return result, mapping

    async def _store_mapping(self, mapping_id: str, mapping: dict[str, str]) -> None:
        key = f"{_PSEUDO_KEY_PREFIX}{mapping_id}"
        await self.valkey.set(key, json.dumps(mapping), ex=_PSEUDO_TTL_SECONDS)

    async def depseudonymize(self, response_text: str, mapping_id: str | None) -> str:
        """Restore real values in a response before it reaches the client."""
        if mapping_id is None:
            return response_text
        raw = await self.valkey.get(f"{_PSEUDO_KEY_PREFIX}{mapping_id}")
        if not raw:
            return response_text
        mapping: dict[str, str] = json.loads(raw)
        result = response_text
        for placeholder, original in mapping.items():
            result = result.replace(placeholder, original)
        return result

    async def cleanup(self, mapping_id: str | None) -> None:
        """Remove the pseudonym map at request end -- it must not outlive the request."""
        if mapping_id is None:
            return
        await self.valkey.delete(f"{_PSEUDO_KEY_PREFIX}{mapping_id}")


def create_upstream_filter(content_filter: Any, valkey: Any) -> UpstreamFilter:
    """Factory for `UpstreamFilter`."""
    return UpstreamFilter(content_filter, valkey)
