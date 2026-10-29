"""Python-vs-Rust proxy parity harness: capture, normalize, redact, compare.

Cutover safety gate for the strangler migration. Responses from the live
Python proxy (and later the Rust proxy) are reduced to a canonical form and
diffed on status, shape and semantic content.

Two normalization modes (``normalize(resp, redact=...)``):

* compare mode (``redact=False``, used by ``diff``): ONLY genuinely volatile
  fields are neutralized (timestamps, latencies, request/trace ids, a
  top-level transport response id). Model ids, identity UUIDs, emails and
  free text are compared verbatim, so cross-tenant leaks surface as diffs.
* disk mode (``redact=True``, used by ``write_snapshot``): additionally
  scrubs secrets/PII so nothing sensitive is ever persisted.

Existing golden-file recording stays in ``snapshot.py``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Volatile at ANY depth: timestamps, latencies, per-request transport ids.
VOLATILE_KEYS = frozenset(
    {
        "request_id",
        "trace_id",
        "span_id",
        "jti",
        "created",
        "created_at",
        "modified_at",
        "updated_at",
        "expires_at",
        "iat",
        "exp",
        "nbf",
        "timestamp",
        "ts",
        "latency",
        "latency_ms",
        "duration",
        "duration_ms",
        "elapsed_ms",
        "processing_ms",
        "system_fingerprint",
        "access_token",
        "refresh_token",
        "key_hash",
    }
)
# A top-level response `id` is volatile ONLY when it looks like a transport id.
_TRANSPORT_ID = re.compile(r"^(chatcmpl|msg|cmpl|resp|req|run)[-_][A-Za-z0-9_-]+$")
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
        "apikey",
        "access_token",
        "refresh_token",
        "token",
        "secret",
        "secret_key",
        "access_key",
        "private_key",
        "client_secret",
        "authorization",
        "key",
    }
)
PII_KEYS = frozenset({"email", "username", "full_name", "phone", "address"})

_BEARER = re.compile(r"Bearer\s+[\w.~+/=-]+", re.I)
_WAKEY = re.compile(r"\bwa-[A-Za-z0-9_]+-[A-Za-z0-9_-]+")
_PROVIDER_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


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


# Pinned Rust "denied" envelope. Rust deliberately makes expired / unknown /
# malformed credentials indistinguishable, and mirrors the Python 401 body
# (snapshots/proxy_chat_completions_unauth.json). P6 re-verifies this against
# the live Rust proxy.
RUST_DENIED_BODY: dict[str, Any] = {"error": "Missing or invalid Bearer token"}
_DENIED_ALLOWED_HEADERS = frozenset({"content-type", "www-authenticate", "vary"})


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
    credential: str | None = None  # kind of credential the divergence applies to


EXPECTED_DIVERGENCES: tuple[Divergence, ...] = (
    Divergence(
        "expired_api_key",
        350,
        "accepts a key whose api_keys.expires_at is in the past (latent bug)",
        "enforces expires_at -> rejects",
        "Python never checks expires_at; Rust does. Remove once gh-350 fixes Python.",
        python_status=200,
        rust_status=401,
        credential="wa-api-key",
    ),
    Divergence(
        "nonnumeric_sub",
        350,
        "accepts JWTs with non-numeric sub/tenant (latent bug)",
        "rejects non-numeric sub/tenant",
        "Python never validates numeric sub/tenant; Rust does. Remove once gh-350 lands.",
        python_status=200,
        rust_status=401,
        credential="jwt",
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
    """Scrub bearer tokens, JWTs, wa-/sk- keys and emails from free text."""
    s = _BEARER.sub("Bearer <REDACTED>", s)
    s = _JWT.sub("<REDACTED_JWT>", s)
    s = _WAKEY.sub("<REDACTED_KEY>", s)
    s = _PROVIDER_KEY.sub("<REDACTED_KEY>", s)
    return _EMAIL.sub("<REDACTED_EMAIL>", s)


def normalize_value(obj: Any, *, redact: bool = False, _top: bool = True) -> Any:
    """Recursively neutralize volatile fields; in redact mode also scrub secrets/PII."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k in sorted(obj):
            lk = k.lower()
            v = obj[k]
            if lk == "id" and _top and isinstance(v, str) and _TRANSPORT_ID.match(v):
                out[k] = "<VOLATILE>"
            elif lk in VOLATILE_KEYS:
                out[k] = "<VOLATILE>"
            elif redact and (lk in SECRET_KEYS or lk in PII_KEYS) and v is not None:
                out[k] = "<REDACTED>"
            else:
                out[k] = normalize_value(v, redact=redact, _top=False)
        return out
    if isinstance(obj, list):
        return [normalize_value(v, redact=redact, _top=False) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj) if redact else obj
    return obj


def parse_sse(body: bytes, *, redact: bool = False) -> list[Any]:
    """Split an SSE body into ordered events.

    ``id:`` lines are ignored (volatile resume cursor). ``retry:`` IS compared:
    it is a semantic reconnection directive a client acts on.
    """
    events: list[Any] = []
    for block in body.decode("utf-8", "replace").replace("\r\n", "\n").split("\n\n"):
        lines = block.split("\n")
        data = "\n".join(ln[5:].lstrip() for ln in lines if ln.startswith("data:"))
        ev = next((ln[6:].strip() for ln in lines if ln.startswith("event:")), None)
        retry = next((ln[6:].strip() for ln in lines if ln.startswith("retry:")), None)
        if not data and ev is None and retry is None:
            continue
        try:
            payload: Any = json.loads(data)
        except ValueError:
            payload = data
        norm = normalize_value(payload, redact=redact)
        if ev is None and retry is None:
            events.append(norm)
        else:
            events.append({"event": ev, "retry": retry, "data": norm})
    return events


def normalize(resp: Response, *, redact: bool = False) -> dict[str, Any]:
    """Canonical form of a response (volatile-neutralized; scrubbed when ``redact``)."""
    headers: dict[str, str] = {}
    for k, v in resp.headers.items():
        lk = k.lower()
        if lk in VOLATILE_HEADERS:
            continue
        if redact and lk in SECRET_HEADERS:
            headers[lk] = "<REDACTED>"
        else:
            headers[lk] = redact_text(v) if redact else v
    body: Any
    if resp.is_sse:
        body = {"sse": parse_sse(resp.body, redact=redact)}
    else:
        try:
            body = normalize_value(json.loads(resp.body), redact=redact) if resp.body else None
        except ValueError:
            text = resp.body.decode("utf-8", "replace")
            body = redact_text(text) if redact else text
    return {"status": resp.status, "headers": headers, "body": body}


def _show(path: str, v: Any) -> str:
    """Render a value for a diff line; sensitive leaves show only a digest."""
    leaf = re.split(r"[.\[]", path)[-1].lower()
    if leaf in SECRET_KEYS or leaf in PII_KEYS or leaf in SECRET_HEADERS:
        return "sha256:" + hashlib.sha256(repr(v).encode()).hexdigest()[:10]
    return repr(v)


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
        out.append(f"{path}: {_show(path, a)} != {_show(path, b)}")


def _check_denied_envelope(rust: Response, status: int, out: list[str]) -> None:
    """Pin the Rust side of a divergence to the exact denied envelope."""
    n = normalize(rust)
    if n["status"] != status:
        out.append(f"$rust.status: {n['status']} != {status}")
    _walk(RUST_DENIED_BODY, n["body"], "$rust.body", out)
    for h, v in n["headers"].items():
        if h not in _DENIED_ALLOWED_HEADERS:
            out.append(f"$rust.headers.{h}: unexpected header on denied response")
        elif h == "content-type" and not v.lower().startswith("application/json"):
            out.append(f"$rust.headers.content-type: {v!r} is not application/json")


def diff(
    python: Response,
    rust: Response,
    *,
    divergence: str | None = None,
    credential: str | None = None,
) -> list[str]:
    """Return human-readable differences (empty = parity).

    With ``divergence`` (a name in EXPECTED_DIVERGENCES, plus the matching
    ``credential`` kind) ONLY the documented status split is excused: Python
    must answer its documented status, and the Rust response must be EXACTLY the
    pinned denied envelope (status, body, headers). Python's success body is the
    behaviour being excused so it is not compared. Anything else on the Rust
    side (a leaky 401 body, extra headers) is reported, never suppressed.
    """
    if divergence is None:
        out: list[str] = []
        _walk(normalize(python), normalize(rust), "$", out)
        return out
    d = _DIVERGENCES[divergence]
    if d.python_status is None or d.rust_status is None:
        raise ValueError(f"{divergence} is a note, not a response divergence")
    if credential != d.credential:
        raise ValueError(f"{divergence} applies to credential={d.credential!r}, got {credential!r}")
    out = []
    if python.status != d.python_status:
        out.append(f"$python.status: {python.status} != {d.python_status}")
    _check_denied_envelope(rust, d.rust_status, out)
    return out


def write_snapshot(path: Path, resp: Response) -> Path:
    """Write the normalized+redacted response; raw secrets never reach disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(normalize(resp, redact=True), indent=2, sort_keys=True) + "\n")
    return path
