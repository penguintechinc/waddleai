"""Hermetic self-test for the Python-vs-Rust parity harness (no live proxy)."""

import json

import pytest

from tests.contract import parity
from tests.contract.parity import Response


def _json(body: dict, status: int = 200, headers: dict | None = None) -> Response:
    return Response(status=status, headers=headers or {}, body=json.dumps(body).encode())


def test_volatile_only_differences_compare_equal() -> None:
    """Self-test: volatile only differences compare equal."""
    a = _json(
        {
            "id": "chatcmpl-aaa",
            "created": 1700000000,
            "latency_ms": 12,
            "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
            "request_id": "11111111-1111-1111-1111-111111111111",
        },
        headers={"x-request-id": "r1", "date": "Mon", "content-type": "application/json"},
    )
    b = _json(
        {
            "id": "chatcmpl-bbb",
            "created": 1800000000,
            "latency_ms": 977,
            "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
            "request_id": "22222222-2222-2222-2222-222222222222",
        },
        headers={"x-request-id": "r2", "date": "Tue", "content-type": "application/json"},
    )
    assert parity.diff(a, b) == []


def test_real_field_difference_compares_unequal() -> None:
    """Self-test: real field difference compares unequal."""
    a = _json({"id": "x", "model": "m", "choices": [{"message": {"content": "hi"}}]})
    b = _json({"id": "y", "model": "m", "choices": [{"message": {"content": "bye"}}]})
    d = parity.diff(a, b)
    assert d and any("content" in x for x in d)
    assert parity.diff(_json({"a": 1}, 200), _json({"a": 1}, 500))
    assert parity.diff(_json({"a": 1}), _json({"a": 1, "extra": 1}))


def _sse(*events: dict, ident: str) -> Response:
    raw = (
        "".join(
            f"data: {json.dumps({**e, 'id': ident, 'created': hash(ident) % 999})}\n\n"
            for e in events
        )
        + "data: [DONE]\n\n"
    )
    return Response(status=200, headers={"content-type": "text/event-stream"}, body=raw.encode())


def test_sse_pair_differing_only_in_ids_compares_equal() -> None:
    """Self-test: sse pair differing only in ids compares equal."""
    evs = [{"delta": "He"}, {"delta": "llo"}]
    assert parity.diff(_sse(*evs, ident="chatcmpl-1"), _sse(*evs, ident="chatcmpl-2")) == []


def test_sse_content_or_order_difference_is_unequal() -> None:
    """Self-test: sse content or order difference is unequal."""
    a = _sse({"delta": "He"}, {"delta": "llo"}, ident="1")
    assert parity.diff(a, _sse({"delta": "llo"}, {"delta": "He"}, ident="1"))
    assert parity.diff(a, _sse({"delta": "He"}, ident="1"))


def test_redaction_strips_auth_and_pii_before_write(tmp_path) -> None:
    """Self-test: redaction strips auth and pii before write."""
    r = Response(
        status=200,
        headers={"Authorization": "Bearer sekrit", "set-cookie": "s=1", "x-api-key": "wa-k-s"},
        body=json.dumps(
            {
                "email": "a@b.com",
                "api_key": "wa-abc-def",
                "ok": 1,
                "note": "mail me a@b.com Bearer abc.def.ghi",
            }
        ).encode(),
    )
    path = parity.write_snapshot(tmp_path / "s.json", r)
    text = path.read_text()
    for leak in ("sekrit", "a@b.com", "wa-abc-def", "wa-k-s", "abc.def.ghi"):
        assert leak not in text
    assert '"ok": 1' in text


@pytest.mark.parametrize("case", parity.EXPECTED_DIVERGENCES, ids=lambda c: c.name)
def test_expected_divergences_documented(case: parity.Divergence) -> None:
    """Self-test: expected divergences documented."""
    assert case.issue == 350 or case.name == "telemetry_user_label"
    assert case.python and case.rust and case.reason


def test_divergence_allowlist_suppresses_only_named_case() -> None:
    """Self-test: divergence allowlist suppresses only named case."""
    py = _json({"error": "ok"}, 200)
    rs = _json({"error": "expired"}, 401)
    assert parity.diff(py, rs)
    assert parity.diff(py, rs, divergence="expired_api_key") == []
    assert parity.diff(py, rs, divergence="nonnumeric_sub") == []
    with pytest.raises(KeyError):
        parity.diff(py, rs, divergence="nope")


def test_divergence_requires_exact_documented_shape() -> None:
    """Self-test: divergence requires exact documented shape."""
    py = _json({"error": "ok"}, 200)
    other = _json({"error": "expired"}, 503)  # not the documented Rust 401 shape
    assert parity.diff(py, other, divergence="expired_api_key")


def test_surface_inventory_is_consistent() -> None:
    """Self-test: surface inventory is consistent."""
    assert parity.SURFACES
    assert {s.status for s in parity.SURFACES} <= {"captured", "pending-rust-proxy"}
    assert all(s.name for s in parity.SURFACES)
