"""
Matrix Factorization Classifier for prompt complexity scoring.

Uses deterministic feature extraction and weighted scoring — no ML model
required.  The classifier examines structural and lexical properties of
the prompt text relative to the declared tool type and returns a discrete
complexity level ("low", "medium", or "high").
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Literal

# ---------------------------------------------------------------------------
# Keyword dictionaries
# ---------------------------------------------------------------------------

CODE_KEYWORDS: List[str] = [
    "function", "class", "def", "return", "import", "from", "const", "let",
    "var", "async", "await", "interface", "struct", "impl", "enum", "trait",
    "lambda", "yield", "decorator", "metaclass", "generic", "template",
    "inheritance", "polymorphism", "abstraction", "encapsulation",
]

SYSTEM_KEYWORDS: List[str] = [
    "sudo", "chmod", "chown", "systemctl", "docker", "kubectl", "helm",
    "iptables", "crontab", "mount", "umount", "ssh", "scp", "rsync",
    "nginx", "apache", "firewall", "selinux", "apparmor", "awk", "sed",
    "grep", "find", "xargs", "pipe", "redirect", "daemon", "service",
]

SQL_KEYWORDS: List[str] = [
    "select", "insert", "update", "delete", "create", "alter", "drop",
    "join", "where", "group by", "having", "order by", "union",
    "subquery", "index", "trigger", "procedure", "transaction",
    "rollback", "commit", "foreign key", "constraint", "partition",
    "window function", "cte", "recursive", "materialized view",
]

# ---------------------------------------------------------------------------
# Tool-type complexity biases
# ---------------------------------------------------------------------------

TOOL_TYPE_WEIGHTS: Dict[str, float] = {
    "bash": 0.15,
    "python": 0.10,
    "javascript": 0.10,
    "typescript": 0.12,
    "go": 0.18,
    "rust": 0.22,
    "java": 0.15,
    "cpp": 0.25,
    "sql": 0.12,
    "web_search": -0.10,
    "file_edit": 0.05,
    "code_review": 0.20,
    "debug": 0.25,
    "test_write": 0.15,
    "documentation": -0.05,
    "refactor": 0.28,
    "architecture": 0.35,
    "data_analysis": 0.18,
    "devops": 0.20,
    "general": 0.0,
}


@dataclass(slots=True, frozen=True)
class ClassificationResult:
    """Immutable result of prompt complexity classification."""

    complexity: Literal["low", "medium", "high"]
    raw_score: float
    feature_scores: Dict[str, float]


class MatrixFactorizationClassifier:
    """Deterministic prompt-complexity classifier.

    Extracts numerical features from the prompt text, applies per-feature
    weights, adds a tool-type bias, and maps the aggregate score to one of
    three complexity buckets.

    Feature dimensions:
        1. Normalised prompt length
        2. Code keyword density
        3. System keyword density
        4. SQL keyword density
        5. Nesting depth (brackets / parentheses)
        6. Line count complexity
        7. Question complexity (interrogative density)
    """

    # Thresholds for mapping raw score -> label
    LOW_THRESHOLD: float = 0.33
    HIGH_THRESHOLD: float = 0.66

    # Per-feature weights (must sum to ~1.0 before tool bias)
    FEATURE_WEIGHTS: Dict[str, float] = {
        "length": 0.12,
        "code_density": 0.20,
        "system_density": 0.15,
        "sql_density": 0.13,
        "nesting": 0.15,
        "line_complexity": 0.10,
        "question_complexity": 0.15,
    }

    def __init__(self) -> None:
        self._code_re = re.compile(
            r"\b(" + "|".join(re.escape(k) for k in CODE_KEYWORDS) + r")\b",
            re.IGNORECASE,
        )
        self._system_re = re.compile(
            r"\b(" + "|".join(re.escape(k) for k in SYSTEM_KEYWORDS) + r")\b",
            re.IGNORECASE,
        )
        self._sql_re = re.compile(
            r"\b(" + "|".join(re.escape(k) for k in SQL_KEYWORDS) + r")\b",
            re.IGNORECASE,
        )
        self._question_re = re.compile(
            r"\b(how|why|what|when|where|which|explain|describe|compare|contrast"
            r"|analyse|analyze|evaluate|design|implement|optimise|optimize)\b",
            re.IGNORECASE,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score(self, prompt: str, tool_type: str) -> str:
        """Return a complexity label for *prompt* given *tool_type*.

        Args:
            prompt: The user-supplied prompt text.
            tool_type: One of the recognised tool-type keys (e.g. ``"python"``,
                ``"bash"``, ``"architecture"``).  Unknown types default to a
                bias of ``0.0``.

        Returns:
            ``"low"``, ``"medium"``, or ``"high"``.
        """
        result = self.score_detailed(prompt, tool_type)
        return result.complexity

    def score_detailed(self, prompt: str, tool_type: str) -> ClassificationResult:
        """Return a full :class:`ClassificationResult` with feature breakdown."""
        features = self._extract_features(prompt)
        tool_bias = TOOL_TYPE_WEIGHTS.get(tool_type.lower(), 0.0)

        weighted_sum = sum(
            features[name] * weight
            for name, weight in self.FEATURE_WEIGHTS.items()
        )
        raw = max(0.0, min(1.0, weighted_sum + tool_bias))

        if raw < self.LOW_THRESHOLD:
            label: Literal["low", "medium", "high"] = "low"
        elif raw < self.HIGH_THRESHOLD:
            label = "medium"
        else:
            label = "high"

        return ClassificationResult(
            complexity=label,
            raw_score=round(raw, 4),
            feature_scores={k: round(v, 4) for k, v in features.items()},
        )

    # ------------------------------------------------------------------
    # Feature extraction
    # ------------------------------------------------------------------

    def _extract_features(self, prompt: str) -> Dict[str, float]:
        """Return a dict of normalised feature values in [0, 1]."""
        words = prompt.split()
        word_count = max(len(words), 1)
        lines = prompt.splitlines()
        line_count = max(len(lines), 1)

        return {
            "length": self._normalise_length(len(prompt)),
            "code_density": self._keyword_density(self._code_re, prompt, word_count),
            "system_density": self._keyword_density(self._system_re, prompt, word_count),
            "sql_density": self._keyword_density(self._sql_re, prompt, word_count),
            "nesting": self._nesting_depth(prompt),
            "line_complexity": self._line_complexity(line_count, word_count),
            "question_complexity": self._keyword_density(
                self._question_re, prompt, word_count
            ),
        }

    # ------------------------------------------------------------------
    # Individual feature helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_length(char_count: int) -> float:
        """Map character count to [0, 1].  Saturates at 5 000 chars."""
        return min(char_count / 5000.0, 1.0)

    @staticmethod
    def _keyword_density(pattern: re.Pattern, text: str, word_count: int) -> float:  # type: ignore[type-arg]
        """Fraction of words matching *pattern*, capped at 1.0."""
        matches = len(pattern.findall(text))
        return min(matches / word_count, 1.0)

    @staticmethod
    def _nesting_depth(text: str) -> float:
        """Normalised maximum bracket/paren nesting depth.

        Tracks ``(``, ``)``, ``[``, ``]``, ``{``, ``}``.  Saturates at
        depth 10.
        """
        max_depth = 0
        current = 0
        openers = frozenset("([{")
        closers = frozenset(")]}")
        for ch in text:
            if ch in openers:
                current += 1
                if current > max_depth:
                    max_depth = current
            elif ch in closers:
                current = max(current - 1, 0)
        return min(max_depth / 10.0, 1.0)

    @staticmethod
    def _line_complexity(line_count: int, word_count: int) -> float:
        """Heuristic combining line count and avg words-per-line."""
        avg_wpl = word_count / line_count
        line_factor = min(line_count / 100.0, 1.0)
        wpl_factor = min(avg_wpl / 20.0, 1.0)
        return (line_factor + wpl_factor) / 2.0
