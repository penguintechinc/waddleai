#!/usr/bin/env python3
"""Seed mock GPU fleet data so the GPU Fleet UI, wizard and e2e specs have something to show.

Writes 4 ``gpu_inventory`` rows (one node per vendor class), 2 ``vllm_deployments`` (one SERVING,
one STOPPED) with timeline events, and 1 ``model_registry`` row (``qwen-12b-awq``, 12B params), all
through penguin-dal and idempotently (re-running adds nothing). It also makes sure the first
organization has a ``vllm_ray`` fleet backend (not counted in the returned totals).

Usage:
    DATABASE_URL=postgresql://user:pass@host:5432/waddleai python3 scripts/seed_gpu_fleet_mock.py
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from datetime import datetime, timedelta
from typing import Any

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

logger = logging.getLogger("seed_gpu_fleet_mock")

BACKEND_NAME = "gpu-fleet-mock"
MODEL_NAME = "qwen-12b-awq"

# (node, class, product, vendor, vram_mb)
_GPUS = [
    ("mock-node-nvidia-1", "nvidia-rtx4090", "NVIDIA GeForce RTX 4090", 1, 24564),
    ("mock-node-nvidia-2", "nvidia-rtx4070", "NVIDIA GeForce RTX 4070", 1, 12282),
    ("mock-node-amd-1", "amd-mi210", "AMD Instinct MI210", 2, 65536),
    ("mock-node-intel-1", "intel-arc-a770", "Intel Arc A770", 3, 16384),
]

# (name, status, replicas, events[(minutes_ago, state, message)])
_DEPLOYMENTS = [
    (
        "mock-qwen-serving",
        5,
        2,
        [
            (30, 1, "queued"),
            (28, 2, "downloading model"),
            (20, 3, "starting engine"),
            (10, 5, "serving"),
        ],
    ),
    ("mock-qwen-stopped", 7, 1, [(60, 5, "serving"), (5, 7, "stopped by an administrator")]),
]


def _first_org_id(db: Any) -> int:
    """Id of the first organization, or a loud failure when the database has none."""
    row = db(db.organizations.id > 0).select(orderby=db.organizations.id, limitby=(0, 1)).first()
    if row is None:
        raise SystemExit("seed_gpu_fleet_mock: no organization exists; create one first")
    return int(row.id)


def _ensure_backend(db: Any, org_id: int) -> int:
    """Return the id of the org's ``vllm_ray`` backend, creating the mock one when absent."""
    table = db.fleet_backends
    existing = (
        db((table.org_id == org_id) & (table.type == "vllm_ray"))
        .select(orderby=table.id, limitby=(0, 1))
        .first()
    )
    if existing is not None:
        return int(existing.id)
    now = datetime.utcnow()
    backend_id = table.insert(
        org_id=org_id,
        name=BACKEND_NAME,
        type="vllm_ray",
        management_scope="full_lifecycle",
        config={"tco": {"kwh_usd": 0.18}, "worker_groups": []},
        status="active",
        created_at=now,
        updated_at=now,
    )
    db.commit()
    return int(backend_id)


def _seed_gpus(db: Any, org_id: int, backend_id: int) -> int:
    """Insert any missing mock GPU rows; returns how many mock GPU rows exist."""
    table = db.gpu_inventory
    for node, gpu_class, product, vendor, vram in _GPUS:
        node_uid = f"uid-{node}"
        query = (table.fleet_backend_id == backend_id) & (table.node_uid == node_uid)
        if db(query & (table.gpu_index == 0)).count() == 0:
            table.insert(
                org_id=org_id,
                fleet_backend_id=backend_id,
                node_uid=node_uid,
                node_name=node,
                gpu_index=0,
                vendor=vendor,
                gpu_class=gpu_class,
                product_name=product,
                vram_total_mb=vram,
                vram_used_mb=0,
                pci_bus_id="0000:01:00.0",
                schedulable=True,
                state=1,
                last_seen=datetime.utcnow(),
            )
    db.commit()
    return sum(
        db((table.fleet_backend_id == backend_id) & (table.node_uid == f"uid-{n[0]}")).count()
        for n in _GPUS
    )


def _seed_model(db: Any) -> int:
    """Insert the ``qwen-12b-awq`` registry row when missing; returns the mock row count."""
    table = db.model_registry
    if db(table.name == MODEL_NAME).count() == 0:
        table.insert(
            name=MODEL_NAME,
            role="general",
            license="apache-2.0",
            origin="mock-origin",
            min_vram=8,
            is_utility=False,
            public_id=str(uuid.uuid4()),
            params_b=12,
            created_at=datetime.utcnow(),
        )
        db.commit()
    return int(db(table.name == MODEL_NAME).count())


def _seed_deployments(db: Any, org_id: int, backend_id: int) -> int:
    """Insert the two mock deployments (and events) when missing; returns the mock count."""
    table = db.vllm_deployments
    events = db.vllm_deployment_events
    for name, status, replicas, timeline in _DEPLOYMENTS:
        if db((table.org_id == org_id) & (table.name == name)).count() > 0:
            continue
        now = datetime.utcnow()
        slug = f"{name}-{uuid.uuid4().hex[:8]}"
        dep_id = table.insert(
            public_id=str(uuid.uuid4()),
            org_id=org_id,
            fleet_backend_id=backend_id,
            name=name,
            slug=slug,
            model_ref=MODEL_NAME,
            served_model_name=MODEL_NAME,
            vendor=1,
            gpu_class="nvidia-rtx4090",
            allow_mixed_class=False,
            quantization=1,
            dtype="bfloat16",
            tensor_parallel=1,
            pipeline_parallel=1,
            replicas_min=1,
            replicas_max=replicas,
            max_model_len=4096,
            status=status,
            serve_app_name=f"d-{slug}",
            endpoint_url=f"http://serve:8000/d/{slug}/v1",
            expose_via_proxy=True,
            comparison_model="openai/gpt-4o-mini",
            price_mode=1,
            price_input_per_1m="0.150000",
            price_output_per_1m="0.600000",
            created_at=now,
            updated_at=now,
        )
        for minutes_ago, state, message in timeline:
            events.insert(
                deployment_id=dep_id,
                ts=now - timedelta(minutes=minutes_ago),
                state=state,
                message=message,
            )
    db.commit()
    return sum(db((table.org_id == org_id) & (table.name == d[0])).count() for d in _DEPLOYMENTS)


def seed(db: Any) -> dict[str, int]:
    """Seed all mock rows idempotently and return the per-table counts of mock rows present."""
    org_id = _first_org_id(db)
    backend_id = _ensure_backend(db, org_id)
    counts = {
        "gpu_inventory": _seed_gpus(db, org_id, backend_id),
        "vllm_deployments": _seed_deployments(db, org_id, backend_id),
        "model_registry": _seed_model(db),
    }
    logger.info(
        "seeded gpu_inventory=%d vllm_deployments=%d model_registry=%d",
        counts["gpu_inventory"],
        counts["vllm_deployments"],
        counts["model_registry"],
    )
    return counts


def main() -> None:
    """Connect to ``DATABASE_URL``, seed, and exit 1 when nothing was seeded."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("seed_gpu_fleet_mock: DATABASE_URL is not set")
    from penguin_dal import DAL  # noqa: PLC0415 -- deferred so --help/imports stay light

    counts = seed(DAL(url))
    if sum(counts.values()) == 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
