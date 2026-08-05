"""
Routing Matrix — database-backed model-selection lookup.

The ``RoutingMatrixEntry`` SQLAlchemy model stores per-(tool_type, complexity,
region) routing decisions.  ``RoutingMatrix`` provides a lookup method with
three-tier fallback: exact match -> wildcard tool_type ("*") -> hard-coded
default.
"""

import logging
from dataclasses import dataclass
from typing import Literal, Optional

from sqlalchemy import Boolean, Column, Float, Integer, String
from sqlalchemy.orm import declarative_base

logger = logging.getLogger(__name__)

Base = declarative_base()


# ---------------------------------------------------------------------------
# SQLAlchemy model
# ---------------------------------------------------------------------------


class RoutingMatrixEntry(Base):  # type: ignore[misc]
    """Persistent routing matrix entry."""

    __tablename__ = "routing_matrix"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    tool_type: str = Column(String(64), nullable=False, index=True)
    complexity: str = Column(String(16), nullable=False, index=True)
    region: str = Column(String(16), nullable=False, index=True)
    model_name: str = Column(String(128), nullable=False)
    model_params: str = Column(String(32), nullable=True)
    vram_gb: float = Column(Float, nullable=True)
    capability_score: float = Column(Float, nullable=True, default=0.5)
    enabled: bool = Column(Boolean, nullable=False, default=True)

    def __repr__(self) -> str:
        return (
            f"<RoutingMatrixEntry tool={self.tool_type} "
            f"complexity={self.complexity} region={self.region} "
            f"model={self.model_name}>"
        )


# ---------------------------------------------------------------------------
# Route decision dataclass
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class RouteDecision:
    """Immutable result returned by routing lookups."""

    model: str
    complexity: Literal["low", "medium", "high"]
    target_type: str
    confidence: float
    reasoning: str


# ---------------------------------------------------------------------------
# Default fallback model
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = "llama3.1:8b"


# ---------------------------------------------------------------------------
# RoutingMatrix lookup class
# ---------------------------------------------------------------------------


class RoutingMatrix:
    """Look up the best model for a given (tool_type, complexity, region).

    Lookup precedence:
        1. Exact match on (tool_type, complexity, region) where enabled=True
        2. Wildcard tool_type ``"*"`` with same (complexity, region)
        3. Hard-coded default model

    The ``db`` parameter is a penguin-dal (PyDAL-compatible) connection.
    """

    def __init__(self, db) -> None:  # type: ignore[type-arg]
        self._db = db

    def lookup(
        self,
        tool_type: str,
        complexity: str,
        region: str,
    ) -> Optional[str]:
        """Return the model name for the best matching route, or ``None``.

        Args:
            tool_type: e.g. ``"python"``, ``"bash"``, ``"architecture"``.
            complexity: ``"low"``, ``"medium"``, or ``"high"``.
            region: e.g. ``"NA"``, ``"EU"``.

        Returns:
            The ``model_name`` string from the best matching enabled entry,
            or ``None`` if no match exists (callers should fall back to a
            default).
        """
        # 1. Exact match
        model = self._query(tool_type, complexity, region)
        if model is not None:
            return model

        # 2. Wildcard tool_type
        model = self._query("*", complexity, region)
        if model is not None:
            logger.debug(
                "Routing wildcard match for (%s, %s, %s) -> %s",
                tool_type,
                complexity,
                region,
                model,
            )
            return model

        # 3. No match — caller decides on fallback
        logger.warning(
            "No routing matrix entry for (%s, %s, %s); returning None",
            tool_type,
            complexity,
            region,
        )
        return None

    def lookup_with_default(
        self,
        tool_type: str,
        complexity: str,
        region: str,
    ) -> str:
        """Like :meth:`lookup` but always returns a model name.

        Falls back to the package-level default (``llama3.1:8b``) when
        no database entry matches.
        """
        return self.lookup(tool_type, complexity, region) or _DEFAULT_MODEL

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _query(
        self,
        tool_type: str,
        complexity: str,
        region: str,
    ) -> Optional[str]:
        """Execute a single lookup against the routing_matrix table."""
        try:
            rows = self._db.executesql(
                "SELECT model_name FROM routing_matrix "
                "WHERE tool_type = %s AND complexity = %s "
                "  AND region = %s AND enabled = true "
                "ORDER BY capability_score DESC "
                "LIMIT 1",
                (tool_type, complexity, region),
            )
            if rows:
                return str(rows[0][0])
        except Exception as exc:
            logger.error(
                "Routing matrix query failed (%s, %s, %s): %s",
                tool_type,
                complexity,
                region,
                exc,
            )
        return None
