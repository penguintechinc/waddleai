#!/usr/bin/env python3
"""
Seed the routing_matrix table with default entries.

Populates 20 tool types x 3 complexities x 2 regions = 120 rows.  Existing
enabled entries for the same (tool_type, complexity, region) triple are
skipped so the script is safe to re-run.

NOTE: Pre-execution safety validation (checking for command injection,
exfiltration, destructive ops, etc.) runs separately with fixed models
(llama3.1:8b for NA, mistral:7b for EU) and is not part of this routing matrix.
See docs/standards/MODEL_ROUTING_MATRIX.md for safety validator details.

Usage:
    DATABASE_URL=postgresql://user:pass@host:5432/waddleai \
        python3 scripts/seed_routing_matrix.py
"""

import os
import sys
from typing import Dict, List, Tuple

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Ensure project root is importable
# ---------------------------------------------------------------------------
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from shared.agents.routing_matrix import Base, RoutingMatrixEntry

# ---------------------------------------------------------------------------
# Tool types
# ---------------------------------------------------------------------------

TOOL_TYPES: List[str] = [
    "bash",
    "python",
    "javascript",
    "typescript",
    "go",
    "rust",
    "java",
    "cpp",
    "sql",
    "web_search",
    "file_edit",
    "code_review",
    "debug",
    "test_write",
    "documentation",
    "refactor",
    "architecture",
    "data_analysis",
    "devops",
    "general",
]

COMPLEXITIES: List[str] = ["low", "medium", "high"]
REGIONS: List[str] = ["NA", "EU"]

# ---------------------------------------------------------------------------
# Model assignments per (complexity, region)
# ---------------------------------------------------------------------------

# NA region models (RTX 4090 optimized - 24GB VRAM max)
_NA_MODELS: Dict[str, Tuple[str, str, float, float]] = {
    # complexity -> (model_name, model_params, vram_gb, capability_score)
    "low": ("llama3.1:8b", "8B", 6.0, 0.40),
    "medium": ("neural-chat:13b", "13B", 12.0, 0.60),
    "high": ("llama3.1:70b-q4_K_M", "70B-q4", 18.0, 0.85),
}

# EU region models (RTX 4090 optimized - 24GB VRAM max)
_EU_MODELS: Dict[str, Tuple[str, str, float, float]] = {
    "low": ("mistral:7b", "7B", 5.0, 0.38),
    "medium": ("mistral:13b", "13B", 13.0, 0.62),
    "high": ("mistral-large:123b-q4_K_M", "123B-q4", 24.0, 0.92),
}

# Per-tool overrides — only specified where the tool warrants a different
# model than the default for its complexity/region.
# All models are RTX 4090 optimized (24GB max) and EU/NA origin only.
_TOOL_OVERRIDES: Dict[Tuple[str, str, str], Tuple[str, str, float, float]] = {
    # (tool_type, complexity, region)

    # Code-centric tools (python, go, rust, cpp, java, typescript, javascript)
    # at high complexity use the largest available models (quantized to fit 4090)
    ("python", "high", "NA"): ("llama3.1:70b-q4_K_M", "70B-q4", 18.0, 0.85),
    ("go", "high", "NA"): ("llama3.1:70b-q4_K_M", "70B-q4", 18.0, 0.85),
    ("rust", "high", "NA"): ("llama3.1:70b-q4_K_M", "70B-q4", 18.0, 0.85),
    ("cpp", "high", "NA"): ("llama3.1:70b-q4_K_M", "70B-q4", 18.0, 0.85),
    ("java", "high", "NA"): ("llama3.1:70b-q4_K_M", "70B-q4", 18.0, 0.85),
    ("typescript", "high", "NA"): ("llama3.1:70b-q4_K_M", "70B-q4", 18.0, 0.85),
    ("javascript", "high", "NA"): ("llama3.1:70b-q4_K_M", "70B-q4", 18.0, 0.85),

    # EU code-heavy high complexity uses Mistral-Large (best available quantized model)
    ("python", "high", "EU"): ("mistral-large:123b-q4_K_M", "123B-q4", 24.0, 0.92),
    ("go", "high", "EU"): ("mistral-large:123b-q4_K_M", "123B-q4", 24.0, 0.92),
    ("rust", "high", "EU"): ("mistral-large:123b-q4_K_M", "123B-q4", 24.0, 0.92),
    ("cpp", "high", "EU"): ("mistral-large:123b-q4_K_M", "123B-q4", 24.0, 0.92),
    ("architecture", "high", "EU"): ("mistral-large:123b-q4_K_M", "123B-q4", 24.0, 0.92),

    # Research/analysis tools stay at medium complexity (don't need huge models)
    ("web_search", "medium", "NA"): ("neural-chat:13b", "13B", 12.0, 0.60),
    ("web_search", "medium", "EU"): ("mistral:13b", "13B", 13.0, 0.62),
    ("data_analysis", "medium", "NA"): ("neural-chat:13b", "13B", 12.0, 0.60),
    ("data_analysis", "medium", "EU"): ("mistral:13b", "13B", 13.0, 0.62),

    # Documentation/refactor at high complexity benefit from large models
    ("documentation", "high", "NA"): ("llama3.1:70b-q4_K_M", "70B-q4", 18.0, 0.85),
    ("documentation", "high", "EU"): ("mistral-large:123b-q4_K_M", "123B-q4", 24.0, 0.92),
    ("refactor", "high", "NA"): ("llama3.1:70b-q4_K_M", "70B-q4", 18.0, 0.85),
    ("refactor", "high", "EU"): ("mistral-large:123b-q4_K_M", "123B-q4", 24.0, 0.92),

    # Code review and testing benefit from detailed reasoning
    ("code_review", "high", "NA"): ("llama3.1:70b-q4_K_M", "70B-q4", 18.0, 0.85),
    ("code_review", "high", "EU"): ("mistral-large:123b-q4_K_M", "123B-q4", 24.0, 0.92),
    ("test_write", "high", "NA"): ("llama3.1:70b-q4_K_M", "70B-q4", 18.0, 0.85),
    ("test_write", "high", "EU"): ("mistral-large:123b-q4_K_M", "123B-q4", 24.0, 0.92),
    ("debug", "high", "NA"): ("llama3.1:70b-q4_K_M", "70B-q4", 18.0, 0.85),
    ("debug", "high", "EU"): ("mistral-large:123b-q4_K_M", "123B-q4", 24.0, 0.92),

    # DevOps at high complexity
    ("devops", "high", "NA"): ("llama3.1:70b-q4_K_M", "70B-q4", 18.0, 0.85),
    ("devops", "high", "EU"): ("mistral-large:123b-q4_K_M", "123B-q4", 24.0, 0.92),
}


def _resolve_model(
    tool_type: str, complexity: str, region: str
) -> Tuple[str, str, float, float]:
    """Return (model_name, model_params, vram_gb, capability_score)."""
    key = (tool_type, complexity, region)
    if key in _TOOL_OVERRIDES:
        return _TOOL_OVERRIDES[key]
    if region == "EU":
        return _EU_MODELS[complexity]
    return _NA_MODELS[complexity]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def seed(database_url: str) -> int:
    """Insert routing matrix entries.  Returns the count of rows inserted."""
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)

    inserted = 0
    with Session(engine) as session:
        for tool_type in TOOL_TYPES:
            for complexity in COMPLEXITIES:
                for region in REGIONS:
                    # Skip if already seeded
                    exists = (
                        session.query(RoutingMatrixEntry)
                        .filter_by(
                            tool_type=tool_type,
                            complexity=complexity,
                            region=region,
                            enabled=True,
                        )
                        .first()
                    )
                    if exists is not None:
                        continue

                    model_name, model_params, vram, cap = _resolve_model(
                        tool_type, complexity, region
                    )
                    entry = RoutingMatrixEntry(
                        tool_type=tool_type,
                        complexity=complexity,
                        region=region,
                        model_name=model_name,
                        model_params=model_params,
                        vram_gb=vram,
                        capability_score=cap,
                        enabled=True,
                    )
                    session.add(entry)
                    inserted += 1

        session.commit()
    return inserted


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable is required.", file=sys.stderr)
        sys.exit(1)

    count = seed(database_url)
    total = len(TOOL_TYPES) * len(COMPLEXITIES) * len(REGIONS)
    print(
        f"Routing matrix seeded: {count} new entries inserted "
        f"({total - count} already existed). Total expected: {total}."
    )


if __name__ == "__main__":
    main()
