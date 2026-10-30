"""Parity oracle: the classification mapper must equal ContentFilter tier-3 on identical input.

The mapper (T5) deliberately copies ContentFilter._run_ner_patterns logic (ruling R2) until P6
deletes the original. This test feeds the SAME raw entities through both and fails on drift.
"""

from __future__ import annotations

import json
from collections import Counter
from concurrent.futures import Executor, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from services.classification.app import mapping
from services.classification.app.engine import RawEntity
from services.classification.app.grpc_gen import classification_pb2 as _pb
from shared.security import content_filter
from shared.security.content_filter import ContentFilter

CORPUS_PATH = Path(__file__).parents[2] / "fixtures" / "classification" / "corpus.json"
pb: Any = _pb  # protoc gencode has no stubs
MAX_CHARS = mapping.MAX_ANALYZED_CHARS

Violation = tuple[str, str, float, str]


def _load_corpus() -> list[dict[str, Any]]:
    """Load the committed corpus (raw entities per text, deterministic, no model)."""
    texts: list[dict[str, Any]] = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))["texts"]
    return texts


CORPUS = _load_corpus()
_ACTION_NAME = {
    pb.ENTITY_ACTION_LOG: "log",
    pb.ENTITY_ACTION_REDACT: "redact",
    pb.ENTITY_ACTION_BLOCK: "block",
}


def _raw(entry: dict[str, Any]) -> list[RawEntity]:
    """Build the raw entities the stub engine would return for a corpus entry."""
    return [
        RawEntity(e["entity_type"], e["start"], e["end"], e["score"]) for e in entry["entities"]
    ]


def _worker_dicts(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The dict contract ``ner_analyze`` returns, as a window-respecting worker emits it.

    Presidio only analyzes the first 2000 code points, so a real worker never reports a span
    starting past the window and cuts a straddling span at it; ``text`` comes from that window.
    """
    analyzed = entry["text"][:MAX_CHARS]
    return [
        {
            "entity_type": e["entity_type"],
            "text": analyzed[e["start"] : min(e["end"], MAX_CHARS)],
            "start": e["start"],
            "end": min(e["end"], MAX_CHARS),
            "score": e["score"],
        }
        for e in entry["entities"]
        if e["start"] < MAX_CHARS
    ]


def _service_set(entry: dict[str, Any]) -> Counter[Violation]:
    """Violations derived from the service mapper, recovering text via the byte offsets."""
    disabled = frozenset(entry["disabled"])
    analyzed_bytes = entry["text"][:MAX_CHARS].encode("utf-8")
    res = mapping.map_spans(entry["text"], _raw(entry), disabled)
    return Counter(
        (
            s.entity_label,
            _ACTION_NAME[s.action],
            round(s.confidence, 6),
            analyzed_bytes[s.start_byte : s.end_byte].decode("utf-8"),
        )
        for s in res.spans
    )


async def _oracle_set(entry: dict[str, Any], mp: pytest.MonkeyPatch) -> Counter[Violation]:
    """Violations from the real ``ContentFilter._run_ner_patterns`` with the model stubbed."""
    dicts = _worker_dicts(entry)
    pool: Executor = ThreadPoolExecutor(max_workers=1)
    cf = object.__new__(ContentFilter)
    cf.ner_filter = object()  # truthy sentinel; model never used

    async def _disabled(org_id: int | None) -> set[str]:
        return set(entry["disabled"])

    mp.setattr(cf, "_load_disabled_ner_entities", _disabled)
    mp.setattr(content_filter, "ner_analyze", lambda _t: dicts)
    mp.setattr(content_filter, "_get_ner_pool", lambda: pool)
    try:
        violations = await cf._run_ner_patterns(entry["text"], "input", None)
    finally:
        pool.shutdown(wait=True)
    return Counter(
        (v.rule_name[len("ner:") :], v.action, round(v.confidence, 6), v.full_matched_text)
        for v in violations
    )


def test_corpus_integrity_and_coverage() -> None:
    """Corpus offsets must point at the recorded match and cover the required edge cases."""
    names = {c["name"] for c in CORPUS}
    for entry in CORPUS:
        for e in entry["entities"]:
            assert entry["text"][e["start"] : e["end"]] == e["match"], entry["name"]
    types = {e["entity_type"] for c in CORPUS for e in c["entities"]}
    assert {"PERSON", "US_SSN", "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD"} <= types
    assert {"LOCATION", "ORG", "NRP", "FOO_BAR"} <= types
    assert {"unicode_emoji", "empty", "no_pii", "long_over_2000_truncated"} <= names
    assert {"duplicate_entity_double_emit_guard", "entity_past_2000_window"} <= names
    assert any(e["start"] >= MAX_CHARS for c in CORPUS for e in c["entities"])
    assert any(len(c["text"]) > MAX_CHARS for c in CORPUS)
    assert any(len(c["text"].encode()) > len(c["text"]) for c in CORPUS)


@pytest.mark.parametrize("entry", CORPUS, ids=[c["name"] for c in CORPUS])
async def test_parity_spans_equal_content_filter_violations(
    entry: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Service mapper output equals ContentFilter tier-3 violations, as multisets (counts match)."""
    service = _service_set(entry)
    oracle = await _oracle_set(entry, monkeypatch)
    assert service == oracle


async def test_parity_is_not_vacuous(monkeypatch: pytest.MonkeyPatch) -> None:
    """Across the corpus both paths find violations AND both drop the same sub-floor ones."""
    kept = dropped = 0
    for entry in CORPUS:
        oracle = await _oracle_set(entry, monkeypatch)
        kept += sum(oracle.values())
        dropped += len(entry["entities"]) - sum(oracle.values())
    print(f"parity non-vacuous: kept={kept} dropped={dropped}")
    assert kept > 0
    assert dropped > 0


def test_corpus_examined_count_printed_and_nonzero() -> None:
    """A zero-item corpus is a failure, never a silent pass (Verification Integrity)."""
    examined = sum(1 for _ in CORPUS)
    print(f"parity corpus examined: {examined} texts")
    assert examined >= 15
