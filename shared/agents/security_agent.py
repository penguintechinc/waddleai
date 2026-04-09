"""
Security Agent — threat evaluation combining RAG pattern matching with
the existing PromptSecurityScanner regex engine.

For every inbound command / prompt the agent:

1. Runs the regex-based :class:`PromptSecurityScanner` for fast
   deterministic detection.
2. Performs a vector similarity search against the ``security_threats``
   RAG collection for known attack patterns.
3. Merges both signals into a unified :class:`SecurityDecision`.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional

from shared.security.prompt_security import Action, PromptSecurityScanner, ThreatDetection
from shared.utils.rag_integration import PgvectorRAGStore, SearchResult

logger = logging.getLogger(__name__)

# Collection name used by the seed script (seed_security_rag.py).
_THREAT_COLLECTION = "security_threats"

# Sensitivity multipliers per tool type — higher means the tool is more
# dangerous if abused.
_TOOL_SENSITIVITY: dict[str, float] = {
    "bash": 1.0,
    "devops": 0.95,
    "sql": 0.90,
    "python": 0.70,
    "go": 0.65,
    "javascript": 0.60,
    "typescript": 0.55,
    "file_edit": 0.50,
    "code_review": 0.25,
    "documentation": 0.15,
    "web_search": 0.20,
    "general": 0.40,
}


@dataclass(slots=True, frozen=True)
class SecurityDecision:
    """Immutable evaluation result from the SecurityAgent."""

    safe: bool
    risk_score: float
    threat_type: Optional[str]
    explanation: str
    blocked: bool
    matched_patterns: List[str]


class SecurityAgent:
    """Evaluate prompts / commands for security threats.

    Combines two detection layers:
    * **Regex layer** — :class:`PromptSecurityScanner` for fast,
      deterministic pattern matching.
    * **RAG layer** — :class:`PgvectorRAGStore` similarity search against
      a pre-seeded ``security_threats`` collection for semantic matching
      of known attack vectors.

    Args:
        db: A PyDAL-compatible database connection (used by both the
            scanner and the RAG store).
        embedding_manager: An :class:`EmbeddingManager` used by the
            PgvectorRAGStore for vector similarity search.
    """

    def __init__(self, db, embedding_manager) -> None:  # type: ignore[type-arg]
        self._scanner = PromptSecurityScanner(db, policy_name="balanced")
        self._rag_store = PgvectorRAGStore(
            write_db=db,
            embedding_manager=embedding_manager,
        )
        self._db = db

    async def evaluate(
        self,
        raw_command: str,
        tool_type: str,
        user_id: Optional[int] = None,
    ) -> SecurityDecision:
        """Analyse *raw_command* for security threats.

        Args:
            raw_command: The prompt or shell command to evaluate.
            tool_type: Declared tool / language type (influences sensitivity).
            user_id: Optional user identifier for audit logging.

        Returns:
            A :class:`SecurityDecision` summarising the risk assessment.
        """
        # ------------------------------------------------------------------
        # Layer 1 — regex-based scanner
        # ------------------------------------------------------------------
        scanner_threats, _sanitized = self._scanner.scan_prompt(raw_command, user_id=user_id)
        regex_score = self._regex_risk_score(scanner_threats)

        # ------------------------------------------------------------------
        # Layer 2 — RAG similarity search
        # ------------------------------------------------------------------
        rag_score, rag_matches = await self._rag_risk_score(raw_command)

        # ------------------------------------------------------------------
        # Merge scores
        # ------------------------------------------------------------------
        sensitivity = _TOOL_SENSITIVITY.get(tool_type.lower(), 0.40)

        # Weighted combination: regex (0.5) + rag (0.3) + sensitivity (0.2)
        combined = regex_score * 0.50 + rag_score * 0.30 + sensitivity * 0.20
        risk_score = round(min(combined, 1.0), 4)

        blocked = risk_score >= 0.8

        # Determine dominant threat type
        threat_type = self._dominant_threat(scanner_threats, rag_matches)

        # Build explanation
        explanation = self._build_explanation(
            regex_score,
            rag_score,
            sensitivity,
            risk_score,
            blocked,
            scanner_threats,
            rag_matches,
            tool_type,
        )

        matched_pattern_strings = [p for t in scanner_threats for p in t.matched_patterns] + [
            r.document.content[:120] for r in rag_matches
        ]

        return SecurityDecision(
            safe=not blocked,
            risk_score=risk_score,
            threat_type=threat_type,
            explanation=explanation,
            blocked=blocked,
            matched_patterns=matched_pattern_strings[:10],
        )

    # ------------------------------------------------------------------
    # Risk-score helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _regex_risk_score(threats: List[ThreatDetection]) -> float:
        """Compute a [0, 1] risk score from regex-based detections."""
        if not threats:
            return 0.0

        max_confidence = max(t.confidence for t in threats)
        blocked_count = sum(1 for t in threats if t.suggested_action == Action.BLOCK)
        threat_count_factor = min(len(threats) / 5.0, 1.0)

        score = max_confidence * 0.50 + (blocked_count / max(len(threats), 1)) * 0.30 + threat_count_factor * 0.20
        return min(score, 1.0)

    async def _rag_risk_score(
        self,
        text: str,
    ) -> tuple[float, List[SearchResult]]:
        """Search the threat RAG collection and derive a risk score."""
        try:
            results: List[SearchResult] = await self._rag_store.search(
                query=text,
                collection=_THREAT_COLLECTION,
                organization_id=0,
                limit=5,
                min_score=0.55,
            )
        except Exception as exc:
            logger.warning("RAG threat search failed: %s", exc)
            return 0.0, []

        if not results:
            return 0.0, []

        # Highest similarity drives the score; additional matches add a
        # diminishing bonus.
        top_score = results[0].score
        additional_bonus = sum(r.score * 0.05 for r in results[1:])
        rag_risk = min(top_score + additional_bonus, 1.0)
        return round(rag_risk, 4), results

    # ------------------------------------------------------------------
    # Threat-type helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _dominant_threat(
        scanner_threats: List[ThreatDetection],
        rag_matches: List[SearchResult],
    ) -> Optional[str]:
        """Pick the single most relevant threat label."""
        if scanner_threats:
            # Highest-confidence regex threat
            best = max(scanner_threats, key=lambda t: t.confidence)
            return best.threat_type.value

        if rag_matches:
            meta = rag_matches[0].document.metadata
            category = meta.get("category") or meta.get("threat_type")
            if category:
                return str(category)
            return "rag_pattern_match"

        return None

    # ------------------------------------------------------------------
    # Explanation builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_explanation(
        regex_score: float,
        rag_score: float,
        sensitivity: float,
        risk_score: float,
        blocked: bool,
        scanner_threats: List[ThreatDetection],
        rag_matches: List[SearchResult],
        tool_type: str,
    ) -> str:
        """Construct a human-readable explanation string."""
        parts: list[str] = []

        if blocked:
            parts.append("REQUEST BLOCKED.")
        else:
            parts.append("Request allowed.")

        parts.append(
            f"Risk score: {risk_score:.2f} "
            f"(regex={regex_score:.2f}, rag={rag_score:.2f}, "
            f"sensitivity[{tool_type}]={sensitivity:.2f})."
        )

        if scanner_threats:
            types = ", ".join(sorted({t.threat_type.value for t in scanner_threats}))
            parts.append(f"Regex detections: {types}.")

        if rag_matches:
            top_sim = rag_matches[0].score
            parts.append(f"RAG matches: {len(rag_matches)} " f"(top similarity={top_sim:.2f}).")

        return " ".join(parts)
