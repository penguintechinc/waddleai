"""Hermetic self-test for the Python-vs-Rust parity harness (no live proxy)."""

import json
from pathlib import Path
from typing import Any

import pytest

from tests.contract import parity
from tests.contract.parity import Response

U1 = "11111111-1111-1111-1111-111111111111"
U2 = "22222222-2222-2222-2222-222222222222"
DENIED = {"error": "Missing or invalid Bearer token"}


def _json(body: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(status=status, headers=headers or {}, body=json.dumps(body).encode())


def _denied(body: Any = None, headers: dict[str, str] | None = None, status: int = 401) -> Response:
    return _json(
        DENIED if body is None else body, status, headers or {"content-type": "application/json"}
    )


def test_volatile_only_differences_compare_equal() -> None:
    """Timestamps, latencies, transport ids and volatile headers are ignored."""
    a = _json(
        {
            "id": "chatcmpl-aaa",
            "created": 1700000000,
            "latency_ms": 12,
            "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
            "request_id": U1,
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
            "request_id": U2,
        },
        headers={"x-request-id": "r2", "date": "Tue", "content-type": "application/json"},
    )
    assert parity.diff(a, b) == []


def test_real_field_difference_compares_unequal() -> None:
    """A real content/status/shape change is reported."""
    a = _json({"id": "x", "model": "m", "choices": [{"message": {"content": "hi"}}]})
    b = _json({"id": "y", "model": "m", "choices": [{"message": {"content": "bye"}}]})
    assert any("content" in x for x in parity.diff(a, b))
    assert parity.diff(_json({"a": 1}, 200), _json({"a": 1}, 500))
    assert parity.diff(_json({"a": 1}), _json({"a": 1, "extra": 1}))


def test_negative_control_model_ids_differ() -> None:
    """/v1/models entries with different ids must NOT compare equal."""
    a = _json({"object": "list", "data": [{"id": "gpt-4o", "object": "model"}]})
    b = _json({"object": "list", "data": [{"id": "llama3", "object": "model"}]})
    assert parity.diff(a, b)


def test_negative_control_identity_uuid_differs() -> None:
    """A different identity UUID (cross-tenant leak shape) must be caught."""
    a = _json({"org": {"public_id": U1}, "note": f"owner {U1}"})
    assert parity.diff(a, _json({"org": {"public_id": U2}, "note": f"owner {U1}"}))
    assert parity.diff(a, _json({"org": {"public_id": U1}, "note": f"owner {U2}"}))
    assert parity.diff(_json({"data": [{"id": U1}]}), _json({"data": [{"id": U2}]}))


def test_negative_control_email_vs_null() -> None:
    """An email value vs null (or a different email) must be caught."""
    a = _json({"email": "a@b.com"})
    assert parity.diff(a, _json({"email": None}))
    assert parity.diff(a, _json({"email": "c@d.com"}))
    assert parity.diff(a, _json({"email": "a@b.com"})) == []


def test_negative_control_free_text_differs() -> None:
    """Differing free text (even id-looking) must be caught."""
    a = _json({"content": "call_alpha did it"})
    assert parity.diff(a, _json({"content": "call_bravo did it"}))


def _sse(*events: dict[str, Any], ident: str, extra: str = "") -> Response:
    raw = "".join(
        f"id: {ident}\n{extra}data: {json.dumps({**e, 'id': 'chatcmpl-' + ident})}\n\n"
        for e in events
    )
    raw += "data: [DONE]\n\n"
    return Response(200, {"content-type": "text/event-stream"}, raw.encode())


def test_sse_pair_differing_only_in_ids_compares_equal() -> None:
    """SSE differing only in event ids / chunk ids is parity."""
    evs = [{"delta": "He"}, {"delta": "llo"}]
    assert parity.diff(_sse(*evs, ident="1"), _sse(*evs, ident="2")) == []


def test_sse_content_or_order_difference_is_unequal() -> None:
    """SSE content, order and count changes are reported."""
    a = _sse({"delta": "He"}, {"delta": "llo"}, ident="1")
    assert parity.diff(a, _sse({"delta": "llo"}, {"delta": "He"}, ident="1"))
    assert parity.diff(a, _sse({"delta": "He"}, ident="1"))


def test_sse_retry_is_compared() -> None:
    """retry: is a reconnection directive, so a differing value is a diff."""
    evs = [{"delta": "x"}]
    assert parity.diff(
        _sse(*evs, ident="1", extra="retry: 1000\n"), _sse(*evs, ident="1", extra="retry: 5000\n")
    )
    assert (
        parity.diff(
            _sse(*evs, ident="1", extra="retry: 1000\n"),
            _sse(*evs, ident="2", extra="retry: 1000\n"),
        )
        == []
    )


_PLANTED = [
    "sekrit-bearer",
    "a@b.com",
    "wa-abc-def",
    "wa-k-s",
    "abc.def.ghi",
    "sk-proj-ABCDEFGH12345678",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sigsigsig",
    "AKIAHIDDENVALUE",
    "hunter2hunter2",
]


def test_redaction_strips_auth_and_secrets_before_write(tmp_path: Path) -> None:
    """Nothing planted (headers, keyed fields, free-text tokens) reaches disk."""
    r = Response(
        status=200,
        headers={
            "Authorization": "Bearer sekrit-bearer",
            "set-cookie": "s=1",
            "x-api-key": "wa-k-s",
        },
        body=json.dumps(
            {
                "email": "a@b.com",
                "api_key": "wa-abc-def",
                "secret_key": "hunter2hunter2",
                "access_key": "AKIAHIDDENVALUE",
                "ok": 1,
                "note": "mail a@b.com Bearer abc.def.ghi key sk-proj-ABCDEFGH12345678",
                "nested": {"jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sigsigsig"},
            }
        ).encode(),
    )
    text = parity.write_snapshot(tmp_path / "s.json", r).read_text()
    for leak in _PLANTED:
        assert leak not in text, leak
    assert '"ok": 1' in text


def test_redact_flag_is_live() -> None:
    """redact=False keeps values comparable; redact=True scrubs them."""
    r = _json({"email": "a@b.com"})
    assert parity.normalize(r)["body"]["email"] == "a@b.com"
    assert parity.normalize(r, redact=True)["body"]["email"] == "<REDACTED>"


def test_diff_output_does_not_echo_sensitive_values() -> None:
    """Diff lines for PII/secret leaves show digests only."""
    out = parity.diff(_json({"email": "a@b.com"}), _json({"email": "c@d.com"}))
    assert out and all("a@b.com" not in x and "c@d.com" not in x for x in out)


@pytest.mark.parametrize("case", parity.EXPECTED_DIVERGENCES, ids=lambda c: c.name)
def test_expected_divergences_documented(case: parity.Divergence) -> None:
    """Every divergence is documented and tied to gh-350 (or is the telemetry note)."""
    assert case.issue == 350 or case.name == "telemetry_user_label"
    assert case.python and case.rust and case.reason


def test_divergence_excuses_only_the_status_split() -> None:
    """The documented 200/401 pair with the exact denied envelope is excused."""
    py = _json({"data": "ok"}, 200)
    assert parity.diff(py, _denied())
    assert parity.diff(py, _denied(), divergence="expired_api_key", credential="wa-api-key") == []
    assert parity.diff(py, _denied(), divergence="nonnumeric_sub", credential="jwt") == []


def test_leaky_401_is_caught_under_divergence() -> None:
    """A 401 carrying leaked data or extra headers is NOT excused (B1 probe)."""
    py = _json({"data": "ok"}, 200)
    leaky = _denied({"error": "LEAKED secret stuff"})
    assert parity.diff(py, leaky, divergence="expired_api_key", credential="wa-api-key")
    extra_key = _denied({**DENIED, "detail": U1})
    assert parity.diff(py, extra_key, divergence="nonnumeric_sub", credential="jwt")
    hdr = _denied(headers={"content-type": "application/json", "x-internal-user": "7"})
    assert parity.diff(py, hdr, divergence="expired_api_key", credential="wa-api-key")
    assert parity.diff(
        py, _denied(status=503), divergence="expired_api_key", credential="wa-api-key"
    )
    assert parity.diff(
        _json({}, 500), _denied(), divergence="expired_api_key", credential="wa-api-key"
    )


def test_divergences_not_interchangeable() -> None:
    """Each divergence is bound to its credential kind; notes/unknowns are rejected."""
    py, rs = _json({"data": "ok"}), _denied()
    with pytest.raises(ValueError):
        parity.diff(py, rs, divergence="expired_api_key", credential="jwt")
    with pytest.raises(ValueError):
        parity.diff(py, rs, divergence="nonnumeric_sub", credential="wa-api-key")
    with pytest.raises(ValueError):
        parity.diff(py, rs, divergence="expired_api_key")
    with pytest.raises(ValueError):
        parity.diff(py, rs, divergence="telemetry_user_label")
    with pytest.raises(KeyError):
        parity.diff(py, rs, divergence="nope")


def test_surface_inventory_is_consistent() -> None:
    """Surface statuses are from the known set."""
    assert parity.SURFACES
    assert {s.status for s in parity.SURFACES} <= {"captured", "pending-rust-proxy"}
    assert all(s.name for s in parity.SURFACES)
