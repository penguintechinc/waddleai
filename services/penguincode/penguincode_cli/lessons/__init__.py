"""Lessons-learned promotion pipeline (penguincode-knowledge-platform, L1/L2).

A "lesson learned" recorded at team visibility (one client engagement) can
be promoted to tenant visibility (shared firm-wide) only after passing this
package's scrub + confidentiality-verification pipeline (`scrub` submodule,
L1). L2 (a separate task) owns the persistence/RPC/client layer that calls
into `generalize_and_scrub`/`verify_scrubbed` -- this package never touches
a database or network itself beyond the injected Ollama client.
"""

from __future__ import annotations

from penguincode_cli.lessons.scrub import (
    LESSONS_PROMOTION_FLAG,
    Finding,
    IssueKind,
    Redaction,
    ScrubResult,
    Verdict,
    generalize_and_scrub,
    verify_scrubbed,
)

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
