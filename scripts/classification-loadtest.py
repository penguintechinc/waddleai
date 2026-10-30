#!/usr/bin/env python3
"""Measure warm-model, cache-MISS Analyze latency against a live classification service.

Reports ``p99_analyze_latency_warm_cache_miss`` (ms). It never relaxes or hides a result:
the budget is printed next to the number and, if exceeded, the exit code is 2 so the
owner can decide (smaller model / more workers). Every request uses unique text so the
content-hash cache can never hit.

Env: CLASSIFICATION_SMOKE_ENDPOINT (host:port), CLASSIFICATION_SMOKE_TOKEN,
CLASSIFICATION_SMOKE_CA_FILE or CLASSIFICATION_SMOKE_INSECURE=1.
"""

from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import grpc

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Spec s11 budget: "a few ms + cache". 10ms is the reporting threshold, not a pass criterion.
DEFAULT_BUDGET_MS = 10.0


def percentile(values: list[float], pct: float) -> float:
    """Return the nearest-rank ``pct`` percentile of ``values`` (must be non-empty)."""
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def build_channel() -> grpc.Channel:
    """Open a TLS (CA file) or explicitly insecure channel from the environment."""
    target = os.environ.get("CLASSIFICATION_SMOKE_ENDPOINT", "").strip()
    if not target:
        raise SystemExit("CLASSIFICATION_SMOKE_ENDPOINT is required")
    if os.environ.get("CLASSIFICATION_SMOKE_INSECURE") == "1":
        return grpc.insecure_channel(target)
    ca = os.environ.get("CLASSIFICATION_SMOKE_CA_FILE", "").strip()
    if not ca:
        raise SystemExit("set CLASSIFICATION_SMOKE_CA_FILE (or CLASSIFICATION_SMOKE_INSECURE=1)")
    return grpc.secure_channel(
        target, grpc.ssl_channel_credentials(root_certificates=Path(ca).read_bytes())
    )


def run(requests: int, warmup: int, concurrency: int, budget_ms: float) -> int:
    """Warm the model, fire unique-text requests, print latency stats; 2 = over budget."""
    token = os.environ.get("CLASSIFICATION_SMOKE_TOKEN", "").strip()
    if not token:
        raise SystemExit("CLASSIFICATION_SMOKE_TOKEN is required")
    meta = [("authorization", f"Bearer {token}")]
    channel = build_channel()
    from services.classification.app.grpc_gen import classification_pb2 as _pb
    from services.classification.app.grpc_gen import classification_pb2_grpc as _pb_grpc

    pb: Any = _pb
    pb_grpc: Any = _pb_grpc
    stub = pb_grpc.ClassificationServiceStub(channel)

    def one() -> float:
        text = f"My name is John Smith, ref {uuid.uuid4().hex}, I live in Paris."
        req = pb.AnalyzeRequest(
            api_version="v1",
            text=text,
            language="en",
            phase=pb.FILTER_PHASE_INPUT,
            org_public_id="00000000-0000-4000-8000-000000000001",
            flag_distinct_id="server",
        )
        start = time.perf_counter()
        resp = stub.Analyze(req, metadata=meta, timeout=30)
        elapsed = (time.perf_counter() - start) * 1000.0
        if resp.cache_hit or resp.degraded:
            raise RuntimeError(
                f"invalid sample: cache_hit={resp.cache_hit} degraded={resp.degraded}"
            )
        return elapsed

    for _ in range(warmup):
        one()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        samples = list(pool.map(lambda _i: one(), range(requests)))
    if not samples:
        print("FAIL: zero samples collected", file=sys.stderr)
        return 1
    p99 = percentile(samples, 99)
    print(
        f"samples={len(samples)} concurrency={concurrency} "
        f"p50_ms={statistics.median(samples):.2f} p95_ms={percentile(samples, 95):.2f} "
        f"max_ms={max(samples):.2f}"
    )
    print(f"p99_analyze_latency_warm_cache_miss_ms={p99:.2f} (budget {budget_ms:.1f} ms)")
    if p99 > budget_ms:
        print("OVER BUDGET: owner decision required (smaller model / more workers)")
        return 2
    return 0


def main() -> int:
    """Parse CLI args and run the load measurement."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--requests", type=int, default=500)
    ap.add_argument("--warmup", type=int, default=20)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--budget-ms", type=float, default=DEFAULT_BUDGET_MS)
    a = ap.parse_args()
    return run(a.requests, a.warmup, a.concurrency, a.budget_ms)


if __name__ == "__main__":
    sys.exit(main())
