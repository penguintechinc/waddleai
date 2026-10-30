"""Pure span mapper: raw NER entities to response ``EntitySpan`` messages.

No I/O. Applies composite confidence, the noise floor and the disabled-label
filter, and emits both code-point and UTF-8 byte offsets into the analyzed
(truncated) text. Matched text is never carried into a span.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.classification.app.engine import RawEntity
from services.classification.app.grpc_gen import classification_pb2 as _pb
from shared.security.ner_filter import ENTITY_CONFIG

# protoc gencode ships no stubs; typed Any so mypy --strict accepts the dynamic module.
pb: Any = _pb
EntitySpan = Any

# Deliberate re-declaration (ruling R2) while both services live; must equal
# shared/security/content_filter.py::_MIN_NER_CONFIDENCE (pinned by a test).
MIN_NER_CONFIDENCE = 0.3
# Presidio analyzes only the first N code points of the input.
MAX_ANALYZED_CHARS = 2000
# Parity with NER_ENTITY_CONFIG.get(type, ("log", 0.60)) for unmapped labels.
_DEFAULT_CONFIG: tuple[str, float] = ("log", 0.60)

_TYPE_BY_LABEL: dict[str, int] = {
    name[len("ENTITY_TYPE_") :]: value
    for name, value in pb.EntityType.items()
    if name not in ("ENTITY_TYPE_UNSPECIFIED", "ENTITY_TYPE_OTHER")
}
_ACTION_BY_NAME: dict[str, int] = {
    "log": pb.ENTITY_ACTION_LOG,
    "redact": pb.ENTITY_ACTION_REDACT,
    "block": pb.ENTITY_ACTION_BLOCK,
}


@dataclass(slots=True)
class MappedResult:
    """Mapped spans plus how much of the input was analyzed and whether it was cut."""

    spans: list[EntitySpan]
    analyzed_chars: int
    truncated: bool


def entity_type_for(label: str) -> tuple[int, str]:
    """Map a Presidio label to its proto enum and label; unknown labels become OTHER."""
    value = _TYPE_BY_LABEL.get(label)
    if value is None:
        return pb.ENTITY_TYPE_OTHER, label
    return value, label


def map_spans(text: str, raw: list[RawEntity], disabled_labels: frozenset[str]) -> MappedResult:
    """Convert raw entities to sorted spans over ``text[:2000]`` with dual offsets.

    Offsets index the truncated string actually analyzed, never the original input.
    """
    analyzed = text[:MAX_ANALYZED_CHARS]
    n = len(analyzed)
    spans: list[EntitySpan] = []
    for ent in raw:
        if ent.entity_type in disabled_labels:
            continue
        action, weight = ENTITY_CONFIG.get(ent.entity_type, _DEFAULT_CONFIG)
        confidence = min(ent.score * weight, 1.0)
        if confidence < MIN_NER_CONFIDENCE:
            continue
        start, end = max(ent.start, 0), min(ent.end, n)
        if start >= end:
            continue
        enum_value, label = entity_type_for(ent.entity_type)
        spans.append(
            pb.EntitySpan(
                entity_type=enum_value,
                entity_label=label,
                start_cp=start,
                end_cp=end,
                start_byte=len(analyzed[:start].encode("utf-8")),
                end_byte=len(analyzed[:end].encode("utf-8")),
                raw_score=ent.score,
                confidence=confidence,
                action=_ACTION_BY_NAME.get(action, pb.ENTITY_ACTION_LOG),
            )
        )
    spans.sort(key=lambda s: (s.start_cp, s.end_cp, s.entity_type, s.entity_label))
    return MappedResult(spans=spans, analyzed_chars=n, truncated=len(text) > MAX_ANALYZED_CHARS)
