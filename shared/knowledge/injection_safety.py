"""Injection-safety gateway (§9.6 + §9.7): write-time filter, read-time re-filter, provenance.

Two choke points every knowledge write and read passes through, reusing the
existing ``ContentFilter`` (PII/PCI tiers) and ``PromptSecurityScanner``
(injection/jailbreak detection) rather than reimplementing detection:

1. ``filter_for_store`` -- content is scanned *before* it is persisted as
   memory/knowledge. An injection payload is caught at store time and never
   marked clean; suspicious writes come back quarantined.
2. ``filter_for_inject`` -- retrieved records are re-scanned at read time
   (defense against pre-existing or scope-promoted poison) and wrapped in a
   provenance-headed quoted block. The result is plain data with no ``role``
   field -- retrieved text is data, never a system/developer instruction; it
   structurally cannot carry role authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from shared.knowledge.scoping import ScopedRecord, TrustTier
from shared.security.content_filter import ContentFilter
from shared.security.prompt_security import PromptSecurityScanner


@dataclass(slots=True)
class StoreFilterResult:
    """Outcome of scanning content before it is persisted."""

    quarantined: bool
    content: str
    """Filtered (PII-redacted) content when allowed; the *original* content
    when quarantined -- a quarantined write is never partially "cleaned"."""
    reason: str | None = None


async def filter_for_store(
    content: str,
    scanner: PromptSecurityScanner,
    content_filter: ContentFilter,
    org_id: int | None = None,
    user_id: int | None = None,
) -> StoreFilterResult:
    """Scan content before it is persisted as knowledge/memory (§9.6 write-time filter).

    Order matters: the injection/jailbreak scan runs first and blocks
    immediately on detection -- the content is never passed on to the PII
    filter, so a blocked write is never partially redacted-and-approved.
    Benign content then passes through ``ContentFilter`` for PII/PCI
    redaction before being marked clean.
    """
    threats, _ = scanner.scan_prompt(content, user_id=user_id)
    if scanner.should_block(threats):
        return StoreFilterResult(quarantined=True, content=content, reason="injection_detected")

    filter_result = await content_filter.filter_input(content, user_id=user_id, org_id=org_id)
    if not filter_result.allowed:
        return StoreFilterResult(quarantined=True, content=content, reason="content_filter_blocked")

    return StoreFilterResult(quarantined=False, content=filter_result.filtered_text)


@dataclass(slots=True)
class InjectableBlock:
    """A provenance-headed quoted block ready to enter a prompt as data.

    Deliberately has no ``role`` field: this is quoted reference material,
    never a system/developer message, so it structurally cannot carry role
    authority regardless of what the underlying content says.
    """

    record_id: str
    text: str
    trust_tier: TrustTier
    token_estimate: int


def _provenance_header(record: ScopedRecord, *, now: datetime | None = None) -> str:
    """Build the provenance header naming scope, author, trust tier, and date (§9.7)."""
    date_str = (record.created_at or now or datetime.utcnow()).strftime("%Y-%m-%d")
    author = record.author_user_id or "unknown"
    if record.trust_tier == TrustTier.UNVERIFIED:
        return f"unverified note captured from user {author}'s session on {date_str}"
    scope_label = getattr(record.scope_type, "value", str(record.scope_type))
    trust_label = record.trust_tier.value
    return f"{trust_label} {scope_label}-scope knowledge from user {author}, recorded {date_str}"


def _format_block(record: ScopedRecord, content: str, *, now: datetime | None = None) -> str:
    """Wrap content in a markdown blockquote with its provenance header.

    A blockquote is structurally quoted material in every chat-template
    rendering -- it is never emitted as a bare instruction line.
    """
    header = _provenance_header(record, now=now)
    quoted_lines = "\n".join(f"> {line}" for line in content.splitlines() or [""])
    return f"> [{header}]\n{quoted_lines}"


async def filter_for_inject(
    records: list[ScopedRecord],
    scanner: PromptSecurityScanner,
    content_filter: ContentFilter,
    org_id: int | None = None,
    *,
    now: datetime | None = None,
) -> list[InjectableBlock]:
    """Re-filter retrieved records and wrap survivors as provenance-headed quoted blocks.

    Defense against pre-existing or scope-promoted poison: every record is
    re-run through the same tiers as ``filter_for_store``, even though it
    was already filtered once at write time. A record that fails now is
    dropped -- never injected -- regardless of why it passed before.
    """
    blocks: list[InjectableBlock] = []
    for record in records:
        threats, _ = scanner.scan_prompt(record.content)
        if scanner.should_block(threats):
            continue

        filter_result = await content_filter.filter_input(record.content, org_id=org_id)
        if not filter_result.allowed:
            continue

        text = _format_block(record, filter_result.filtered_text, now=now)
        blocks.append(
            InjectableBlock(
                record_id=record.id,
                text=text,
                trust_tier=record.trust_tier,
                token_estimate=max(1, len(text) // 4),
            )
        )
    return blocks


__all__ = ["StoreFilterResult", "InjectableBlock", "filter_for_store", "filter_for_inject"]
