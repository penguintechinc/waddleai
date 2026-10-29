"""Python-vs-Rust proxy parity harness: capture, normalize, redact, compare.

Cutover safety gate for the strangler migration. Responses from the live
Python proxy (and later the Rust proxy) are reduced to a canonical form with
volatile fields neutralized and secrets/PII redacted, then diffed on status,
shape and semantic content. Existing golden-file recording stays in
``snapshot.py``; this module adds the cross-implementation comparator.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VOLATILE_KEYS = frozenset(
    {
        "id",
        "request_id",
        "trace_id",
        "span_id",
        "created",
        "created_at",
        "modified_at",
        "updated_at",
        "expires_at",
        "iat",
        "exp",
        "nbf",
        "jti",
        "timestamp",
        "ts",
        "time",
        "latency",
        "latency_ms",
        "duration",
        "duration_ms",
        "elapsed_ms",
        "processing_ms",
        "system_fingerprint",
        "key_id",
        "access_token",
        "refresh_token",
        "token",
        "key_hash",
    }
)
VOLATILE_HEADERS = frozenset(
    {
        "date",
        "x-request-id",
        "x-trace-id",
        "traceparent",
        "tracestate",
        "server",
        "content-length",
        "x-response-time",
        "x-process-time",
        "etag",
        "age",
        "keep-alive",
        "connection",
        "transfer-encoding",
    }
)
SECRET_HEADERS = frozenset(
    {"authorization", "x-api-key", "cookie", "set-cookie", "proxy-authorization"}
)
SECRET_KEYS = frozenset(
    {
        "password",
        "api_key",
        "access_token",
        "refresh_token",
        "token",
        "secret",
        "client_secret",
        "authorization",
        "key",
    }
)
PII_KEYS = frozenset({"email", "username", "full_name", "phone", "address"})

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_BEARER = re.compile(r"Bearer\s+[\w.~+/=-]+", re.I)
_WAKEY = re.compile(r"\bwa-[A-Za-z0-9_]+-[A-Za-z0-9_-]+")
_PREFIXED_ID = re.compile(r"\b(chatcmpl|msg|cmpl|req|resp|call|toolu|run)[-_][A-Za-z0-9_-]{4,}")


@dataclass(slots=True)
class Response:
    """One captured HTTP response (status, headers, raw body bytes)."""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    @property
    def is_sse(self) -> bool:
        """True when the content-type marks a server-sent-event stream."""
        ct = {k.lower(): v for k, v in self.headers.items()}.get("content-type", "")
        return "text/event-stream" in ct


@dataclass(slots=True, frozen=True)
class Divergence:
    """A deliberate, documented Python-vs-Rust behavioural difference."""

    name: str
    issue: int | None
    python: str
    rust: str
    reason: str
    python_status: int | None = None
    rust_status: int | None = None


EXPECTED_DIVERGENCES: tuple[Divergence, ...] = (
    Divergence(
        "expired_api_key",
        350,
        "accepts a key whose api_keys.expires_at is in the past (latent bug)",
        "enforces expires_at -> rejects",
        "Python never checks expires_at; Rust does. Remove once gh-350 fixes Python.",
        python_status=200,
        rust_status=401,
    ),
    Divergence(
        "nonnumeric_sub",
        350,
        "accepts JWTs with non-numeric sub/tenant (latent bug)",
        "rejects non-numeric sub/tenant",
        "Python never validates numeric sub/tenant; Rust does. Remove once gh-350 lands.",
        python_status=200,
        rust_status=401,
    ),
    Divergence(
        "telemetry_user_label",
        None,
        "metric label `user` is only ever 'unknown' (deliberately dropped)",
        "same: not a diff target",
        "Parity NOTE only: not an HTTP response difference, never compared by diff().",
    ),
)
_DIVERGENCES = {d.name: d for d in EXPECTED_DIVERGENCES}


@dataclass(slots=True, frozen=True)
class Surface:
    """A proxy surface and whether it can be captured yet."""

    name: str
    status: str  # "captured" | "pending-rust-proxy"
    note: str = ""


SURFACES: tuple[Surface, ...] = (
    Surface("proxy /health + /readyz", "captured", "test_proxy_health.py"),
    Surface("proxy /v1/chat/completions (ok/malformed/unauth)", "captured", "golden snapshots"),
    Surface("proxy /v1/messages (malformed/unauth)", "captured", "golden snapshots"),
    Surface("proxy /v1/models (list/unauth)", "captured", "golden snapshots"),
    Surface("proxy /mem0/* (CRUD + scope)", "captured", "golden snapshots"),
    Surface(
        "management /api/v1/* auth/orgs/keys/quotas/routing",
        "captured",
        "Python-only; stays Python",
    ),
    Surface(
        "proxy streaming chat (SSE)",
        "pending-rust-proxy",
        "diff() supports SSE; no live capture yet",
    ),
    Surface("proxy /v1/embeddings", "pending-rust-proxy", "no golden snapshot yet"),
    Surface(
        "proxy cross-impl diff (Python vs Rust)",
        "pending-rust-proxy",
        "needs Rust proxy; P6 adds capture adapter",
    ),
)


def redact_text(s: str) -> str:
    """Scrub bearer tokens, wa- keys and emails from free text."""
    s = _BEARER.sub("Bearer <REDACTED>", s)
    s = _WAKEY.sub("<REDACTED_KEY>", s)
    return _EMAIL.sub("<REDACTED_EMAIL>", s)


def _norm_text(s: str) -> str:
    s = redact_text(s)
    s = _UUID.sub("<UUID>", s)
    return _PREFIXED_ID.sub(lambda m: f"{m.group(1)}-<ID>", s)


def normalize_value(obj: Any, *, redact: bool = True) -> Any:
    """Recursively neutralize volatile fields and redact secrets/PII."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k in sorted(obj):
            lk = k.lower()
            if lk in SECRET_KEYS or lk in PII_KEYS:
                out[k] = "<REDACTED>"
            elif lk in VOLATILE_KEYS:
                out[k] = "<VOLATILE>"
            else:
                out[k] = normalize_value(obj[k], redact=redact)
        return out
    if isinstance(obj, list):
        return [normalize_value(v, redact=redact) for v in obj]
    if isinstance(obj, str):
        return _norm_text(obj)
    return obj


def parse_sse(body: bytes) -> list[Any]:
    """Split an SSE body into ordered event payloads (JSON-decoded when possible)."""
    events: list[Any] = []
    for block in body.decode("utf-8", "replace").replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(ln[5:].lstrip() for ln in block.split("\n") if ln.startswith("data:"))
        ev = next((ln[6:].strip() for ln in block.split("\n") if ln.startswith("event:")), None)
        if not data and ev is None:
            continue
        try:
            payload: Any = json.loads(data)
        except ValueError:
            payload = data
        events.append(
            {"event": ev, "data": normalize_value(payload)} if ev else normalize_value(payload)
        )
    return events


def normalize(resp: Response) -> dict[str, Any]:
    """Canonical, redacted, volatile-free form of a response."""
    headers = {
        k.lower(): ("<REDACTED>" if k.lower() in SECRET_HEADERS else _norm_text(v))
        for k, v in resp.headers.items()
        if k.lower() not in VOLATILE_HEADERS
    }
    body: Any
    if resp.is_sse:
        body = {"sse": parse_sse(resp.body)}
    else:
        try:
            body = normalize_value(json.loads(resp.body)) if resp.body else None
        except ValueError:
            body = _norm_text(resp.body.decode("utf-8", "replace"))
    return {"status": resp.status, "headers": headers, "body": body}


def _walk(a: Any, b: Any, path: str, out: list[str]) -> None:
    if type(a) is not type(b):
        out.append(f"{path}: type {type(a).__name__} != {type(b).__name__}")
    elif isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}.{k}: missing in python")
            elif k not in b:
                out.append(f"{path}.{k}: missing in rust")
            else:
                _walk(a[k], b[k], f"{path}.{k}", out)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} != {len(b)}")
        for i, (x, y) in enumerate(zip(a, b, strict=False)):  # length diff already reported
            _walk(x, y, f"{path}[{i}]", out)
    elif a != b:
        out.append(f"{path}: {a!r} != {b!r}")


def diff(python: Response, rust: Response, *, divergence: str | None = None) -> list[str]:
    """Return human-readable differences (empty = parity).

    ``divergence`` names an entry of EXPECTED_DIVERGENCES; when the pair has
    exactly its documented statuses the diff is suppressed, otherwise it is
    reported normally (an undocumented shape is never silently excused).
    """
    if divergence is not None:
        d = _DIVERGENCES[divergence]
        if d.python_status is None or d.rust_status is None:
            raise ValueError(f"{divergence} is a note, not a response divergence")
        if python.status == d.python_status and rust.status == d.rust_status:
            return []
    out: list[str] = []
    _walk(normalize(python), normalize(rust), "$", out)
    return out


def write_snapshot(path: Path, resp: Response) -> Path:
    """Write the normalized+redacted response; raw secrets never reach disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(normalize(resp), indent=2, sort_keys=True) + "\n")
    return path
