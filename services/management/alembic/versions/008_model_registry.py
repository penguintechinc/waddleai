"""Model registry: dual-default seed + provider_credentials.plan_budget.

Adds `model_registry` (§2.2/§2.3 platform spec): the catalog of admissible
model weights, carrying the `license`/`origin` metadata the PRC-origin
deny-list and dual-default license-admissibility test are checked against
at registration and at fleet `place_model` time. Seeded here with the
current §2.3 dual-default set -- the routing-classifier role moved from
Gemma 3 to Gemma 4 (`gemma4:e2b`, now Apache-2.0) in this pass of the spec,
so this seed intentionally does NOT match the older `gemma3:1b` /
`granite3.3:2b` example text in the phase-1 plan doc; the spec table (§2.3)
is authoritative and is what's reproduced below. All seeded rows are
`is_utility=True` (routing classifier / security auditor / embeddings are
internal-function roles, excluded from Free-tier model-count caps, §2.4
Q#7) and all origins clear the §2.2 deny-list (no PRC-origin entries).

Also adds `provider_credentials.plan_budget` (JSON) -- the §7.3 window-based
plan-budget config the token gate reads; rate limiting itself is the
Cilium-edge branch (§12.1, `organizations.rpm_limit` added by 007).

NOTE for the next migration author: per §13.1, `009a_response_cache`
(PR #124, developed in parallel against down_revision="006_add_memory_scope")
is expected to be re-parented onto this revision (`008_model_registry`) at
merge time, continuing the `006 -> 007 -> 008 -> 009a -> 009b` chain. Do not
hand-edit 009a/009b from this branch.

Revision ID: 008_model_registry
Revises: 007_drop_ailb_add_native_limits
Create Date: 2026-08-14
"""

from datetime import datetime
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008_model_registry"
down_revision: Union[str, None] = "007_drop_ailb_add_native_limits"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# §2.3 dual-default set. Kept in sync manually with the spec table -- this
# migration is a fixed historical seed, not a live view of the spec.
_DUAL_DEFAULT_SEED = [
    # Routing classifier -- Gemma 4 ships Apache-2.0, so the dual-default
    # requirement no longer applies to this role; alternatives are still
    # registered so they remain selectable.
    {
        "name": "gemma4:e2b",
        "role": "routing_classifier",
        "license": "Apache-2.0",
        "origin": "Google",
        "min_vram": 2,
        "ollama_tag": "gemma4:e2b",
        "is_utility": True,
    },
    {
        "name": "phi4-mini",
        "role": "routing_classifier",
        "license": "MIT",
        "origin": "Microsoft",
        "min_vram": 4,
        "ollama_tag": "phi4-mini",
        "is_utility": True,
    },
    {
        "name": "smollm2:1.7b",
        "role": "routing_classifier",
        "license": "Apache-2.0",
        "origin": "HuggingFace",
        "min_vram": 2,
        "ollama_tag": "smollm2:1.7b",
        "is_utility": True,
    },
    # Security auditor / intent classifier (text) -- ShieldGemma is
    # Gemma-2-based and remains Gemma ToU, so it keeps the dual-default
    # requirement; Apache-2.0 alternatives are the Granite Guardian family.
    {
        "name": "shieldgemma:2b",
        "role": "security_auditor",
        "license": "Gemma ToU",
        "origin": "Google",
        "min_vram": 2,
        "ollama_tag": "shieldgemma:2b",
        "is_utility": True,
    },
    {
        "name": "granite3-guardian:2b",
        "role": "security_auditor",
        "license": "Apache-2.0",
        "origin": "IBM",
        "min_vram": 2,
        "ollama_tag": "granite3-guardian:2b",
        "is_utility": True,
    },
    {
        "name": "granite4.1-guardian",
        "role": "security_auditor",
        "license": "Apache-2.0",
        "origin": "IBM",
        "min_vram": 8,
        "ollama_tag": "granite4.1-guardian",
        "is_utility": True,
    },
    # Embeddings -- already Apache-2.0, no dual-default requirement.
    {
        "name": "nomic-embed-text",
        "role": "embeddings",
        "license": "Apache-2.0",
        "origin": "Nomic",
        "min_vram": 1,
        "ollama_tag": "nomic-embed-text",
        "is_utility": True,
    },
    {
        "name": "mxbai-embed-large",
        "role": "embeddings",
        "license": "Apache-2.0",
        "origin": "Mixedbread AI",
        "min_vram": 1,
        "ollama_tag": "mxbai-embed-large",
        "is_utility": True,
    },
]


def upgrade() -> None:
    op.create_table(
        "model_registry",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("role", sa.String(100), nullable=False),
        sa.Column("license", sa.String(100), nullable=False),
        sa.Column("origin", sa.String(100), nullable=False),
        sa.Column("min_vram", sa.Integer(), nullable=True),
        sa.Column("ollama_tag", sa.String(255), nullable=True),
        sa.Column("resolved_digest", sa.String(128), nullable=True),
        sa.Column("is_utility", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("idx_modreg_role", "model_registry", ["role"])

    model_registry_table = sa.table(
        "model_registry",
        sa.column("name", sa.String),
        sa.column("role", sa.String),
        sa.column("license", sa.String),
        sa.column("origin", sa.String),
        sa.column("min_vram", sa.Integer),
        sa.column("ollama_tag", sa.String),
        sa.column("resolved_digest", sa.String),
        sa.column("is_utility", sa.Boolean),
        sa.column("created_at", sa.DateTime),
    )
    now = datetime.utcnow()
    op.bulk_insert(
        model_registry_table,
        [{**row, "resolved_digest": None, "created_at": now} for row in _DUAL_DEFAULT_SEED],
    )

    op.add_column("provider_credentials", sa.Column("plan_budget", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("provider_credentials", "plan_budget")
    op.drop_index("idx_modreg_role", table_name="model_registry")
    op.drop_table("model_registry")
