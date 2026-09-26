"""Lessons-learned scrub + confidentiality verifier (T-L1).

Product model (consulting analogy): a *tenant* is the firm, a *team* is a
client engagement. A "lesson learned" recorded at team visibility (scoped to
one client engagement) may be promoted to *tenant* visibility -- shared
firm-wide -- but ONLY after every client-confidential specific has been
removed. This module is that guarantee. It is the safety core of the whole
lessons-promotion feature: a false "clean" verdict here is a client-
confidentiality breach, not a cosmetic bug.

Pipeline, per :func:`generalize_and_scrub`:

1. **LLM generalization** -- the configured Ollama orchestration model
   (mirrors ``graphs.knowledge``/``graphs.memory``'s extraction pattern) is
   prompted to rewrite the lesson as a client-agnostic, transferable insight,
   stripping client/company/person/project names and engagement-specific
   detail while keeping the generalizable lesson. The model's output is
   untrusted input: it is asked for a single JSON object via Ollama's
   ``format="json"`` mode, but a local model can still wrap it in prose,
   markdown fences, or emit a malformed/incomplete response.
   ``_parse_generalized_text()`` never raises -- any parse/shape failure (or
   an ``httpx.HTTPError`` from the call itself) blocks the promotion outright
   (returns an empty ``generalized_text`` with ``verdict.clean=False``)
   rather than silently falling back to the raw, un-generalized input.
2. **Deterministic redaction** over the LLM's output: emails (including a
   common ``user [at] domain [dot] tld`` obfuscation), phone numbers,
   obvious secrets/API keys/tokens (vendor-prefixed formats plus a
   gitleaks-style generic ``key: value`` pattern -- see the repo root
   ``.gitleaks.toml``), SSN-like numbers, IPv4 addresses, and URLs that
   embed a known client identifier. Each removed span is recorded as a
   :class:`Redaction` carrying only its *kind* -- never the matched value.
   This step deliberately does NOT attempt to strip bare (non-URL) mentions
   of a client/org/team name -- that is free-form prose regex cannot chase
   reliably, and is the LLM generalization step's job (with
   :func:`verify_scrubbed` below as the backstop when it fails).
3. **Confidentiality verification** (:func:`verify_scrubbed`) re-scans the
   redacted text with the same detection primitives PLUS an explicit check
   for the source tenant/org/team's own name(s)/id(s) (from ``ctx`` and, if
   present, ``source_metadata``). This is the independent safety net for
   the one thing regex redaction cannot reliably do on its own: catch a
   client name the LLM's semantic generalization step failed to strip. Any
   residual finding -> ``clean=False``. Fail-closed: empty/unparseable input
   is NOT clean; only a positive, evidence-based scan result is clean.

**Flag-gated**: :data:`LESSONS_PROMOTION_FLAG` (default OFF) is checked
first, before any LLM call -- off means a blocked, no-op result, never a
silent promotion.

**No PII/secret values ever appear in logs, spans, or the returned
:class:`Redaction`/:class:`Finding` objects** -- only counts and closed-set
``kind`` labels. This holds even when ``verdict.clean`` is ``False``: callers
receive *that a finding exists*, never the confidential value that produced
it.

**Caller contract**: only promote (share tenant-wide) when
``result.verdict.clean is True``. A non-clean ``ScrubResult`` still carries
the best-effort ``generalized_text`` (LLM-generalized + deterministically
redacted, never the raw original) so a human reviewer can see what's left to
fix -- but it must never be treated as safe for firm-wide sharing on its
own.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.config.settings import Settings
from penguincode_cli.flags.client import is_enabled
from penguincode_cli.observability.otel import timed_store_operation
from penguincode_cli.ollama.client import OllamaClient
from penguincode_cli.ollama.types import Message

logger = logging.getLogger(__name__)

#: Flag key gating the entire lessons-promotion scrub pipeline (default OFF,
#: per the platform's flag/license conventions -- `flags/client.py`).
LESSONS_PROMOTION_FLAG = "penguincode.lessons-promotion"

#: Defensive cap on prompt size, mirroring `graphs.knowledge`/`graphs.memory`.
_MAX_INPUT_CHARS = 6000

#: Identifiers shorter than this are skipped by the client-identifier checks
#: -- a 1-2 char tenant/org id (common in tests/fixtures) would otherwise
#: match almost any text, making the check meaningless. Real tenant/org/team
#: ids are UUIDs, far longer than this floor.
_MIN_IDENTIFIER_LEN = 3

_METADATA_NAME_KEYS = (
    "tenant_name",
    "org_name",
    "team_name",
    "client_name",
    "company_name",
    "customer_name",
    "engagement_name",
    "project_name",
)

_GENERALIZATION_SYSTEM_PROMPT = (
    "You are a lessons-learned generalization engine for a consulting firm. "
    "The user's text is a lesson learned recorded during ONE client engagement. "
    "Rewrite it as a CLIENT-AGNOSTIC, transferable lesson suitable for firm-wide "
    "sharing with people who have no connection to that engagement. Remove every "
    "client name, company name, person name, project/engagement name, and any "
    "other engagement-specific specific (dates, dollar amounts, contract terms, "
    "internal system/tool names, locations). KEEP the generalizable insight, root "
    "cause, and recommended action -- that is the entire value of the rewrite. "
    "Respond with ONLY a JSON object of this exact shape, no other text, no "
    'markdown fences:\n{"generalized_text": "..."}\n'
    "If the lesson cannot be perfectly generalized, still produce your best "
    "rewrite -- never leave a client-identifying detail in place to preserve "
    "meaning."
)


class IssueKind(StrEnum):
    """Closed set of redaction/finding categories -- kept bounded and safe to log."""

    EMAIL = "email"
    PHONE = "phone"
    SECRET = "secret"
    URL = "url"
    IP = "ip"
    SSN = "ssn"
    CLIENT_IDENTIFIER = "client_identifier"
    FLAG_DISABLED = "flag_disabled"
    GENERALIZATION_FAILED = "generalization_failed"


@dataclass(slots=True, frozen=True)
class Redaction:
    """One item removed from the generalized text.

    Carries only a closed-set `kind` and a short, non-identifying `label`
    (e.g. the pattern name that matched) -- NEVER the matched value itself,
    so a `Redaction` is always safe to log, store, or hand back to a caller
    without itself becoming a confidentiality leak.
    """

    kind: IssueKind
    label: str


@dataclass(slots=True, frozen=True)
class Finding:
    """One residual confidentiality issue detected by :func:`verify_scrubbed`.

    Same "no values, ever" contract as `Redaction` -- `detail` is a static,
    human-readable description of *why* the finding fired, never the
    matched/offending text.
    """

    kind: IssueKind
    detail: str


@dataclass(slots=True, frozen=True)
class Verdict:
    """The confidentiality verifier's outcome: clean-or-not, plus why not.

    Fail-closed by construction: `clean` is only ever `True` when a scan
    actually ran and found nothing -- there is no code path that defaults
    `clean` to `True` on doubt, error, or missing input.
    """

    clean: bool
    findings: list[Finding] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class ScrubResult:
    """Output of :func:`generalize_and_scrub`.

    `generalized_text` is empty whenever nothing promotable was produced
    (flag off, empty input, or a failed/malformed LLM generalization) --
    never the raw, un-generalized original. When generalization succeeds,
    `generalized_text` is the LLM-generalized + deterministically-redacted
    text regardless of `verdict.clean` (so a non-clean result is still
    reviewable by a human) -- callers MUST gate actual firm-wide promotion
    on `verdict.clean is True`, never on `generalized_text` being non-empty.
    """

    generalized_text: str
    redactions: list[Redaction]
    verdict: Verdict


# ---------------------------------------------------------------------------
# Deterministic detection patterns (shared by redaction and verification).
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

#: Common manual-obfuscation convention: `jane.doe [at] acmecorp [dot] com`.
#: Requires bracket/paren delimiters around `at`/`dot` so ordinary prose
#: ("look at the dot on the map") can never match.
_EMAIL_OBFUSCATED_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+\s*[\[\(]\s*at\s*[\]\)]\s*[A-Za-z0-9.\-]+"
    r"\s*[\[\(]\s*dot\s*[\]\)]\s*[A-Za-z]{2,}\b",
    re.IGNORECASE,
)

_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)")

_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

_IPV4_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"
)

_URL_RE = re.compile(r"https?://[^\s)\]}'\"<>]+")

#: Obvious secrets/API keys/tokens -- vendor-prefixed formats plus a
#: gitleaks-style generic `key/token/secret: value` assignment pattern
#: (mirrors the repo root `.gitleaks.toml` generic-api-key rule). Each entry
#: is `(pattern_name, compiled_pattern)`.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,72}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")),
    (
        "bearer_token",
        re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{10,}=*", re.IGNORECASE),
    ),
    (
        "vendor_prefixed_secret",
        re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test|proj)?[-_]?[A-Za-z0-9]{16,64}\b"),
    ),
    (
        "generic_keyed_secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|api[_-]?token|secret|password|passwd|"
            r"access[_-]?key|auth[_-]?token|token)\b\s*[:=]\s*"
            r"['\"]?([A-Za-z0-9\-_./+]{12,150})['\"]?"
        ),
    ),
)

#: `(kind, pattern, label)` -- used by :func:`verify_scrubbed` to re-scan for
#: every deterministic-redaction category (SSN/secrets/email/phone/IP).
#: Client identifiers are checked separately (they depend on `ctx`/
#: `source_metadata`, not a fixed pattern).
_DETECTION_PATTERNS: tuple[tuple[IssueKind, re.Pattern[str], str], ...] = (
    (IssueKind.SSN, _SSN_RE, "SSN-like number"),
    *((IssueKind.SECRET, pattern, name) for name, pattern in _SECRET_PATTERNS),
    (IssueKind.EMAIL, _EMAIL_OBFUSCATED_RE, "obfuscated email address"),
    (IssueKind.EMAIL, _EMAIL_RE, "email address"),
    (IssueKind.PHONE, _PHONE_RE, "phone number"),
    (IssueKind.IP, _IPV4_RE, "IP address"),
)


def _collect_identifier_terms(
    ctx: ScopeContext, source_metadata: dict[str, Any] | None
) -> list[str]:
    """Source tenant/org/team/user identifiers (ids + any known names) to check for.

    Ids come straight from `ctx` (the hard-boundary scope handle every
    caller already has); names, when available, come from `source_metadata`
    under a small set of conventional keys. Terms shorter than
    `_MIN_IDENTIFIER_LEN` are dropped -- see that constant's docstring.
    """
    raw_terms: list[Any] = [ctx.tenant_id, ctx.user_id, ctx.org_id, *ctx.team_ids]
    if source_metadata:
        for key in _METADATA_NAME_KEYS:
            raw_terms.append(source_metadata.get(key))

    seen: set[str] = set()
    terms: list[str] = []
    for term in raw_terms:
        cleaned = term.strip() if isinstance(term, str) else ""
        if len(cleaned) < _MIN_IDENTIFIER_LEN:
            continue
        dedup_key = cleaned.lower()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        terms.append(cleaned)
    return terms


def _contains_any_identifier(text: str, terms: list[str]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def _redact_all(
    text: str, pattern: re.Pattern[str], kind: IssueKind, label: str
) -> tuple[str, list[Redaction]]:
    """Replace every match of `pattern` with a `[KIND]` placeholder, recording each hit."""
    redactions: list[Redaction] = []
    placeholder = f"[{kind.value.upper()}]"

    def _sub(_match: re.Match[str]) -> str:
        redactions.append(Redaction(kind=kind, label=label))
        return placeholder

    return pattern.sub(_sub, text), redactions


def _redact_client_urls(text: str, terms: list[str]) -> tuple[str, list[Redaction]]:
    """Redact whole URLs that embed a known source tenant/org/team identifier."""
    if not terms:
        return text, []
    lowered_terms = [t.lower() for t in terms]
    redactions: list[Redaction] = []

    def _sub(match: re.Match[str]) -> str:
        url = match.group(0)
        if any(term in url.lower() for term in lowered_terms):
            redactions.append(Redaction(kind=IssueKind.URL, label="client_identifying_url"))
            return "[URL]"
        return url

    return _URL_RE.sub(_sub, text), redactions


def _apply_deterministic_redactions(
    text: str, ctx: ScopeContext, source_metadata: dict[str, Any] | None
) -> tuple[str, list[Redaction]]:
    """Run every deterministic redaction pass over `text`, in a fixed order.

    Secrets and SSNs go first (structured, unambiguous). Client-identifying
    URLs are redacted whole (a masked identifier inside an otherwise-intact
    URL can still be identifying via path/host structure) -- this is the
    only client-identifier redaction performed here; bare (non-URL)
    mentions are intentionally left to the LLM generalization step, with
    :func:`verify_scrubbed` as the backstop (see module docstring). Email/
    phone/IP round out the pass.
    """
    redactions: list[Redaction] = []

    text, hits = _redact_all(text, _SSN_RE, IssueKind.SSN, "ssn")
    redactions.extend(hits)

    for name, pattern in _SECRET_PATTERNS:
        text, hits = _redact_all(text, pattern, IssueKind.SECRET, name)
        redactions.extend(hits)

    terms = _collect_identifier_terms(ctx, source_metadata)

    text, hits = _redact_client_urls(text, terms)
    redactions.extend(hits)

    text, hits = _redact_all(text, _EMAIL_OBFUSCATED_RE, IssueKind.EMAIL, "email_obfuscated")
    redactions.extend(hits)
    text, hits = _redact_all(text, _EMAIL_RE, IssueKind.EMAIL, "email")
    redactions.extend(hits)

    text, hits = _redact_all(text, _PHONE_RE, IssueKind.PHONE, "phone")
    redactions.extend(hits)

    text, hits = _redact_all(text, _IPV4_RE, IssueKind.IP, "ip")
    redactions.extend(hits)

    return text, redactions


def verify_scrubbed(
    ctx: ScopeContext,
    text: str,
    *,
    source_metadata: dict[str, Any] | None = None,
) -> Verdict:
    """The confidentiality verifier -- the final gate before firm-wide sharing.

    Re-scans `text` for every deterministic-redaction category (PII,
    secrets/keys) PLUS an explicit check for the source tenant/org/team's
    own identifiers (ids from `ctx`; names, if available, from
    `source_metadata`) -- this second check is what catches a client name
    the LLM generalization step failed to strip, which regex redaction
    alone cannot reliably do for free-form prose.

    Fail-closed: empty/whitespace-only `text` is NOT clean (nothing was
    actually verified, so there is no basis to call it clean). This
    function never raises and never returns `clean=True` on anything other
    than a completed scan that found zero issues.
    """
    if not text or not text.strip():
        return Verdict(
            clean=False,
            findings=[
                Finding(
                    kind=IssueKind.GENERALIZATION_FAILED,
                    detail="nothing to verify -- text is empty",
                )
            ],
        )

    findings: list[Finding] = []
    for kind, pattern, label in _DETECTION_PATTERNS:
        if pattern.search(text):
            findings.append(Finding(kind=kind, detail=f"residual {label} detected"))

    terms = _collect_identifier_terms(ctx, source_metadata)
    if _contains_any_identifier(text, terms):
        findings.append(
            Finding(
                kind=IssueKind.CLIENT_IDENTIFIER,
                detail="residual source tenant/org/team identifier detected",
            )
        )

    return Verdict(clean=not findings, findings=findings)


def _blocked_result(kind: IssueKind, detail: str) -> ScrubResult:
    """A never-promotable result -- flag off, empty input, or a failed/malformed LLM call."""
    return ScrubResult(
        generalized_text="",
        redactions=[],
        verdict=Verdict(clean=False, findings=[Finding(kind=kind, detail=detail)]),
    )


async def _call_llm(client: OllamaClient, model: str, text: str) -> str:
    """Prompt `model` to generalize `text`, accumulating the streamed JSON response."""
    messages = [
        Message(role="system", content=_GENERALIZATION_SYSTEM_PROMPT),
        Message(role="user", content=text),
    ]
    response_text = ""
    async for chunk in client.chat(model=model, messages=messages, stream=True, format="json"):
        if chunk.message and chunk.message.content:
            response_text += chunk.message.content
    return response_text


def _extract_json_block(raw: str) -> str | None:
    """Best-effort recovery of one JSON object/array from non-pure-JSON text.

    Same strategy as `graphs.knowledge`/`graphs.memory`'s helper of the same
    name (duplicated here rather than imported -- this module stays
    self-contained, matching this codebase's existing convention of
    per-extractor copies rather than a shared private helper): handles a
    leading/trailing sentence or a ```json ... ``` fence, tracking whether
    it is inside a JSON string (respecting `\\"` escapes) so braces inside
    string values don't throw off the bracket count.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text[:4].lower() == "json":
            text = text[4:].lstrip()

    start_idx = next((i for i, c in enumerate(text) if c in "{["), None)
    if start_idx is None:
        return None
    open_char = text[start_idx]
    close_char = "}" if open_char == "{" else "]"

    depth = 0
    in_string = False
    escape = False
    for i in range(start_idx, len(text)):
        c = text[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == open_char:
            depth += 1
        elif c == close_char:
            depth -= 1
            if depth == 0:
                return text[start_idx : i + 1]
    return None


def _parse_generalized_text(raw: str) -> str | None:
    """Parse the LLM's `{"generalized_text": "..."}` response. Never raises.

    Returns `None` on any parse/shape failure -- the caller treats that as a
    hard block, never as license to fall back to the raw input.
    """
    parsed: Any = None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        block = _extract_json_block(raw)
        if block is not None:
            try:
                parsed = json.loads(block)
            except (json.JSONDecodeError, ValueError):
                parsed = None

    if not isinstance(parsed, dict):
        return None
    text = parsed.get("generalized_text")
    if not isinstance(text, str) or not text.strip():
        return None
    return text


async def generalize_and_scrub(
    ctx: ScopeContext,
    content: str,
    *,
    source_metadata: dict[str, Any] | None = None,
    ollama_client: OllamaClient | None = None,
    settings: Settings | None = None,
) -> ScrubResult:
    """Generalize `content` into a client-agnostic lesson and verify it's clean.

    Gated on `LESSONS_PROMOTION_FLAG` -- off returns a blocked `ScrubResult`
    immediately, with no LLM call. `settings`/`ollama_client` are optional
    test/reuse seams, mirroring `graphs.knowledge`/`graphs.memory` --
    production defaults are built from `Settings()` when omitted.

    Never raises on a bad LLM call or malformed LLM output -- both degrade
    to a blocked result (see `_blocked_result`), never a silent
    pass-through of the raw, un-generalized `content`. `source_metadata`,
    when given, supplies human-readable client-identifying names (e.g.
    `org_name`/`client_name`) alongside the ids already available on `ctx`,
    for both the deterministic redaction pass and the final
    `verify_scrubbed` call.
    """
    if not is_enabled(LESSONS_PROMOTION_FLAG, ctx):
        logger.info("lessons-promotion scrub blocked: %s is off", LESSONS_PROMOTION_FLAG)
        return _blocked_result(IssueKind.FLAG_DISABLED, "lessons-promotion flag is disabled")

    if not content or not content.strip():
        return _blocked_result(IssueKind.GENERALIZATION_FAILED, "empty lesson content")

    cfg = settings or Settings()
    truncated = content[:_MAX_INPUT_CHARS]

    with timed_store_operation("extraction", "lessons.scrub", backend="ollama"):
        try:
            if ollama_client is not None:
                raw_response = await _call_llm(ollama_client, cfg.models.orchestration, truncated)
            else:
                async with OllamaClient(
                    base_url=cfg.ollama.api_url, timeout=cfg.ollama.timeout
                ) as client:
                    raw_response = await _call_llm(client, cfg.models.orchestration, truncated)
        except httpx.HTTPError as exc:
            logger.warning(
                "lessons-promotion scrub: Ollama call failed (%s); blocking",
                type(exc).__name__,
            )
            return _blocked_result(IssueKind.GENERALIZATION_FAILED, "generalization call failed")

        generalized = _parse_generalized_text(raw_response)
        if generalized is None:
            logger.warning("lessons-promotion scrub: malformed LLM output; blocking")
            return _blocked_result(
                IssueKind.GENERALIZATION_FAILED, "malformed generalization output"
            )

        scrubbed_text, redactions = _apply_deterministic_redactions(
            generalized, ctx, source_metadata
        )
        verdict = verify_scrubbed(ctx, scrubbed_text, source_metadata=source_metadata)

        logger.info(
            "lessons-promotion scrub complete: %d redaction(s), verdict.clean=%s, %d finding(s)",
            len(redactions),
            verdict.clean,
            len(verdict.findings),
        )

    return ScrubResult(generalized_text=scrubbed_text, redactions=redactions, verdict=verdict)


__all__ = [
    "LESSONS_PROMOTION_FLAG",
    "Finding",
    "IssueKind",
    "Redaction",
    "ScrubResult",
    "Verdict",
    "generalize_and_scrub",
    "verify_scrubbed",
]
