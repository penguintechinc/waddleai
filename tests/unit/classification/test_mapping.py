"""Unit tests for the pure span mapper (composite confidence, floor, dual offsets)."""

from __future__ import annotations

import pytest

from services.classification.app import mapping
from services.classification.app.engine import RawEntity
from services.classification.app.grpc_gen import classification_pb2 as pb
from shared.security import content_filter


def _map(
    text: str, raw: list[RawEntity], disabled: frozenset[str] = frozenset()
) -> mapping.MappedResult:
    return mapping.map_spans(text, raw, disabled)


def test_composite_confidence_is_min_raw_times_weight() -> None:
    """Verify: composite confidence is min raw times weight."""
    res = _map("Alice Smith", [RawEntity("PERSON", 0, 11, 0.85)])
    assert len(res.spans) == 1
    assert res.spans[0].confidence == pytest.approx(0.697, abs=1e-6)
    assert res.spans[0].raw_score == pytest.approx(0.85)
    assert res.spans[0].entity_type == pb.ENTITY_TYPE_PERSON
    assert res.spans[0].action == pb.ENTITY_ACTION_REDACT


def test_composite_confidence_capped_at_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify: the min(..., 1.0) clamp fires when a weight exceeds 1.0."""
    monkeypatch.setitem(mapping.ENTITY_CONFIG, "PERSON", ("redact", 1.5))
    res = _map("Alice Smith", [RawEntity("PERSON", 0, 11, 0.9)])  # 0.9 * 1.5 = 1.35
    assert res.spans[0].confidence == 1.0


_ENUM_TABLE = [
    ("PERSON", 1), ("US_SSN", 2), ("US_PASSPORT", 3), ("US_DRIVER_LICENSE", 4),
    ("MEDICAL_LICENSE", 5), ("EMAIL_ADDRESS", 6), ("PHONE_NUMBER", 7), ("CREDIT_CARD", 8),
    ("IBAN_CODE", 9), ("CRYPTO", 10), ("LOCATION", 11), ("IP_ADDRESS", 12), ("URL", 13),
    ("NRP", 14), ("DATE_TIME", 15), ("ORG", 16), ("NOT_A_REAL_LABEL", 99),
]  # fmt: skip


@pytest.mark.parametrize(("label", "value"), _ENUM_TABLE)
def test_entity_type_for_pinned_to_frozen_proto_ints(label: str, value: int) -> None:
    """Verify: every label maps to its frozen proto int; unknown maps to OTHER=99."""
    assert mapping.entity_type_for(label) == (value, label)


def test_enum_table_covers_every_entity_config_label() -> None:
    """Verify: the pinned table covers every ENTITY_CONFIG label (drift guard)."""
    assert {lbl for lbl, v in _ENUM_TABLE if v != 99} == set(mapping.ENTITY_CONFIG)


def test_zero_length_and_out_of_range_spans_dropped() -> None:
    """Verify: empty, inverted and fully-truncated spans are dropped."""
    raw = [
        RawEntity("PERSON", 5, 5, 0.9),
        RawEntity("PERSON", 8, 3, 0.9),
        RawEntity("PERSON", 2100, 2200, 0.9),
    ]
    assert _map("a" * 2500, raw).spans == []


def test_below_floor_composite_dropped_but_raw_above_floor() -> None:
    """Verify: floor applies to composite, not raw (raw 0.35 * 0.85 = 0.2975)."""
    assert _map("D1234567", [RawEntity("US_DRIVER_LICENSE", 0, 8, 0.35)]).spans == []


def test_sort_tiebreaker_is_deterministic() -> None:
    """Verify: identical offsets order by entity_type then label, any input order."""
    a, b = RawEntity("PERSON", 0, 5, 0.9), RawEntity("EMAIL_ADDRESS", 0, 5, 0.9)
    r1 = _map("abcde", [a, b]).spans
    r2 = _map("abcde", [b, a]).spans
    assert [s.entity_label for s in r1] == [s.entity_label for s in r2]
    assert [s.entity_type for s in r1] == [1, 6]


def test_floor_pinned_to_content_filter() -> None:
    """Verify: floor pinned to content filter."""
    assert mapping.MIN_NER_CONFIDENCE == content_filter._MIN_NER_CONFIDENCE == 0.3


def test_below_floor_dropped() -> None:
    """Verify: below floor dropped."""
    res = _map("D1234567", [RawEntity("US_DRIVER_LICENSE", 0, 8, 0.3)])
    assert res.spans == []


def test_disabled_label_skipped() -> None:
    """Verify: disabled label skipped."""
    raw = [RawEntity("PERSON", 0, 5, 0.9), RawEntity("EMAIL_ADDRESS", 6, 12, 0.9)]
    res = _map("Alice abcdef", raw, frozenset({"PERSON"}))
    assert [s.entity_label for s in res.spans] == ["EMAIL_ADDRESS"]


def test_unknown_label_gets_log_0_60_default_and_OTHER_enum() -> None:  # noqa: N802
    """Verify: unknown label gets log 0 60 default and OTHER enum."""
    res = _map("whatever", [RawEntity("WEIRD_THING", 0, 8, 0.5)])
    span = res.spans[0]
    assert span.entity_type == pb.ENTITY_TYPE_OTHER
    assert span.entity_label == "WEIRD_THING"
    assert span.action == pb.ENTITY_ACTION_LOG
    assert span.confidence == pytest.approx(0.5 * 0.60)
    assert mapping.entity_type_for("WEIRD_THING") == (pb.ENTITY_TYPE_OTHER, "WEIRD_THING")
    assert mapping.entity_type_for("PERSON") == (pb.ENTITY_TYPE_PERSON, "PERSON")


def test_offsets_are_codepoints_and_utf8_bytes() -> None:
    """Verify: offsets are codepoints and utf8 bytes."""
    text = "é 😀 Alice Smith"
    start_cp = text.index("Alice")
    end_cp = len(text)
    res = _map(text, [RawEntity("PERSON", start_cp, end_cp, 0.9)])
    span = res.spans[0]
    assert (span.start_cp, span.end_cp) == (start_cp, end_cp)
    assert span.start_byte != span.start_cp
    got = text.encode("utf-8")[span.start_byte : span.end_byte].decode("utf-8")
    assert got == text[span.start_cp : span.end_cp] == "Alice Smith"


def test_truncation_reported_beyond_2000_chars() -> None:
    """Verify: truncation reported beyond 2000 chars."""
    text = "a" * 2500
    raw = [RawEntity("PERSON", 1990, 2000, 0.9), RawEntity("PERSON", 1995, 2400, 0.9)]
    res = _map(text, raw)
    assert res.truncated is True
    assert res.analyzed_chars == 2000
    assert all(s.end_cp <= 2000 for s in res.spans)
    short = _map("abc", [])
    assert (short.truncated, short.analyzed_chars) == (False, 3)


def test_offsets_index_truncated_string_bytes() -> None:
    """Verify: offsets index truncated string bytes."""
    text = "😀" * 2100
    res = _map(text, [RawEntity("PERSON", 1998, 2000, 0.9)])
    span = res.spans[0]
    assert span.start_byte == 1998 * 4 and span.end_byte == 2000 * 4


def test_span_has_no_text_field() -> None:
    """Verify: span has no text field."""
    names = {f.name for f in pb.EntitySpan.DESCRIPTOR.fields}
    assert "text" not in names


def test_spans_sorted_by_start_then_end() -> None:
    """Verify: spans sorted by start then end."""
    raw = [
        RawEntity("PERSON", 10, 15, 0.9),
        RawEntity("PERSON", 0, 9, 0.9),
        RawEntity("EMAIL_ADDRESS", 0, 4, 0.9),
    ]
    res = _map("a" * 30, raw)
    assert [(s.start_cp, s.end_cp) for s in res.spans] == [(0, 4), (0, 9), (10, 15)]
