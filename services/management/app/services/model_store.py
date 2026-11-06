"""Model store: weight pull Jobs, progress events and weights teardown (Plan C, C3b).

Weights live once per org and model at ``<store>/<org_id>/<model_ref>`` on the RWX model-store
volume; each deployment sees them through the ``<store>/<slug>`` symlink Plan B's backend checks.
``ensure_weights`` skips the pull when the digest marker exists; otherwise it starts one pull Job
per (cluster, model) and parks the deployment in ``PULLING``. ``PullPoller`` (driven by the
reconcile loop) writes a progress event every 10 s, deploys when the weights land, and cleans up
the short-lived HF-token Secret. The token is never logged, put in args, or written to events.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from shared.fleet.enums import DeploymentStatus, LocalDeploymentKind
from shared.security.credential_encryption import decrypt_credential

from . import fleet_secrets
from .model_store_job import build_pull_job_manifest

logger = logging.getLogger(__name__)

DIGEST_MARKER = ".digest"
PROGRESS_FILE = ".progress"
PROGRESS_INTERVAL_S = 10.0
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$")
S = DeploymentStatus


class WeightsInUse(Exception):  # noqa: N818 -- name fixed by the plan
    """Weights cannot be deleted while a deployment references them."""

    def __init__(self, deployments: list[tuple[int, str]]) -> None:
        """Carry the referencing ``(kind, public_id)`` pairs (never numeric ids)."""
        super().__init__("weights in use")
        self.deployments = deployments


def store_root() -> str:
    """Model-store mount point (chart-provided; ``/models`` by default)."""
    return os.environ.get("WADDLEAI_MODEL_STORE_PATH", "/models")


def pull_image() -> str:
    """Image the pull Job runs (the management image, set by the chart)."""
    return os.environ.get("WADDLEAI_MODEL_PULL_IMAGE", os.environ.get("WADDLEAI_IMAGE", ""))


def safe_model_ref(model_ref: str) -> str:
    """Return ``model_ref`` if it is a safe relative path (no traversal), else ``ValueError``."""
    if not _SAFE_REF.fullmatch(model_ref) or ".." in model_ref.split("/"):
        raise ValueError("invalid model reference")
    return model_ref


def canonical_path(org_id: int, model_ref: str) -> str:
    """Weights directory ``<store>/<org_id>/<model_ref>``."""
    return f"{store_root()}/{int(org_id)}/{safe_model_ref(model_ref)}"


def job_name(org_id: int, model_ref: str) -> str:
    """Deterministic pull Job name: one Job per (cluster, org, model)."""
    digest = hashlib.sha256(f"{org_id}/{model_ref}".encode()).hexdigest()[:12]
    return f"waddleai-pull-{digest}"


def weights_present(path: str) -> bool:
    """True when the digest marker exists (only a completed pull publishes it)."""
    return Path(path, DIGEST_MARKER).is_file()


def read_progress(path: str) -> tuple[int, int]:
    """``(bytes_done, bytes_total)`` from the pull's progress file; ``(0, 0)`` when absent."""
    try:
        data = json.loads(Path(path, PROGRESS_FILE).read_text())
        return int(data["bytes_done"]), int(data["bytes_total"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0, 0


def link_slug(path: str, slug: str) -> None:
    """Point ``<store>/<slug>`` at the canonical weights (replaces a stale link)."""
    link = Path(store_root(), slug)
    tmp = link.with_name(link.name + ".tmp")
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(path)
    os.replace(tmp, link)


def _now() -> datetime:
    """Naive UTC timestamp for DAL columns."""
    return datetime.utcnow()


def _event(db: Any, deployment_id: int, state: int, message: str) -> None:
    """Append a deployment event."""
    db.vllm_deployment_events.insert(
        deployment_id=deployment_id, ts=_now(), state=int(state), message=message
    )


def _set_status(db: Any, deployment_id: int, status: int, detail: str | None) -> None:
    """Persist a deployment status transition."""
    db(db.vllm_deployments.id == deployment_id).update(
        status=int(status), status_detail=detail, updated_at=_now()
    )


def _backend_hf_token(db: Any, deployment_id: int) -> str | None:
    """Decrypted ``credentials_ref`` of the deployment's backend (gated-repo HF token)."""
    dep = db(db.vllm_deployments.id == deployment_id).select().first()
    backend = db(db.fleet_backends.id == dep.fleet_backend_id).select().first()
    if backend is None or not backend.credentials_ref:
        return None
    return decrypt_credential(backend.credentials_ref) or None


async def ensure_weights(
    db: Any,
    org_id: int,
    model_ref: str,
    deployment_id: int,
    kind: int,
    *,
    slug: str | None = None,
) -> str:
    """Make the weights available; returns the store path (the row is PULLING if not yet there).

    Present (digest marker) -> link the slug and return without a Job or event. Absent -> create
    the HF-token Secret (gated repos only), start the pull Job, mark the deployment PULLING.
    """
    path = canonical_path(org_id, model_ref)
    if await asyncio.to_thread(weights_present, path):
        if slug:
            await asyncio.to_thread(link_slug, path, slug)
        return path
    if kind != LocalDeploymentKind.VLLM:
        raise ValueError("model pulls are only supported for vLLM deployments")
    token = await asyncio.to_thread(_backend_hf_token, db, deployment_id)
    secret_name: str | None = None
    gateway = fleet_secrets.get_gateway()
    namespace = fleet_secrets.namespace()
    if token:
        secret_name = f"waddleai-hf-{deployment_id}"
        await asyncio.to_thread(gateway.create_secret, namespace, secret_name, {"HF_TOKEN": token})
    manifest = build_pull_job_manifest(
        name=job_name(org_id, model_ref),
        namespace=namespace,
        image=pull_image(),
        model_ref=model_ref,
        store_path=path,
        secret_name=secret_name,
        store_root=store_root(),
    )
    await asyncio.to_thread(gateway.create_job, namespace, manifest)

    def _mark() -> None:
        _set_status(db, deployment_id, S.PULLING, None)
        _event(db, deployment_id, S.PULLING, "pulling model weights")

    await asyncio.to_thread(_mark)
    return path


class PullPoller:
    """Advances PENDING/PULLING deployments: progress events, deploy on completion, cleanup."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """Keep the per-deployment last-progress-event time on the injected clock."""
        self._clock = clock
        self._last_event: dict[int, float] = {}

    async def __call__(self, db: Any, backend: Any, dep: Any) -> bool:
        """Return True when the deployment may proceed to a normal Serve reconcile."""
        path = canonical_path(dep.org_id, dep.model_ref)
        if await asyncio.to_thread(weights_present, path):
            return await self._finish(db, backend, dep, path)
        gateway = fleet_secrets.get_gateway()
        namespace = fleet_secrets.namespace()
        state = await asyncio.to_thread(
            gateway.job_state, namespace, job_name(dep.org_id, dep.model_ref)
        )
        if state == "failed" or state == "missing":
            await self._fail(db, dep, f"model pull {state}")
            return False
        now = self._clock()
        last = self._last_event.get(dep.id)
        if last is None or now - last >= PROGRESS_INTERVAL_S:
            done, total = await asyncio.to_thread(read_progress, path)
            self._last_event[dep.id] = now
            await asyncio.to_thread(
                _event,
                db,
                dep.id,
                S.PULLING,
                json.dumps({"bytes_done": done, "bytes_total": total}),
            )
        return False

    async def _finish(self, db: Any, backend: Any, dep: Any, path: str) -> bool:
        """Weights landed: clean up the token Secret, link the slug and deploy."""
        gateway = fleet_secrets.get_gateway()
        namespace = fleet_secrets.namespace()
        await asyncio.to_thread(gateway.delete_secret, namespace, f"waddleai-hf-{dep.id}")
        await asyncio.to_thread(link_slug, path, dep.slug)
        self._last_event.pop(dep.id, None)
        try:
            await backend.deploy(dep)
        except RuntimeError:
            logger.warning("deploy after pull failed for deployment %s", dep.id)
        return False

    async def _fail(self, db: Any, dep: Any, message: str) -> None:
        """Mark the deployment FAILED, drop the token Secret, and record why."""
        gateway = fleet_secrets.get_gateway()
        await asyncio.to_thread(
            gateway.delete_secret, fleet_secrets.namespace(), f"waddleai-hf-{dep.id}"
        )

        def _mark() -> None:
            _set_status(db, dep.id, S.FAILED, message)
            _event(db, dep.id, S.FAILED, message)

        await asyncio.to_thread(_mark)
        dep.status = int(S.FAILED)


def _references(db: Any, org_id: int, model_ref: str) -> list[tuple[int, str]]:
    """``(kind, public_id)`` of this org's deployments using the model, across all three kinds.

    Ownership is derived only through ``fleet_backend_id -> fleet_backends.org_id`` (vLLM rows
    carry ``org_id`` directly). Other orgs' rows, and rows with no derivable owner, are never
    counted or returned, so a refusal can never reveal another tenant's deployments.
    """
    refs: list[tuple[int, str]] = []
    for row in db(
        (db.vllm_deployments.org_id == org_id) & (db.vllm_deployments.model_ref == model_ref)
    ).select():
        refs.append((int(LocalDeploymentKind.VLLM), str(row.public_id)))
    backend_ids = [int(r.id) for r in db(db.fleet_backends.org_id == org_id).select()]
    if not backend_ids:
        return refs
    owned_ollama = {
        int(r.id): r
        for r in db(db.ollama_deployments.fleet_backend_id.belongs(backend_ids)).select()
    }
    if owned_ollama:
        for model in db(
            (db.ollama_models.model_name == model_ref)
            & db.ollama_models.deployment_id.belongs(list(owned_ollama))
        ).select():
            refs.append(
                (
                    int(LocalDeploymentKind.OLLAMA),
                    str(owned_ollama[int(model.deployment_id)].public_id),
                )
            )
    for row in db(
        (db.llamacpp_deployments.model_name == model_ref)
        & db.llamacpp_deployments.fleet_backend_id.belongs(backend_ids)
    ).select():
        refs.append((int(LocalDeploymentKind.LLAMACPP), str(row.public_id)))
    return refs


async def delete_weights(db: Any, org_id: int, model_ref: str) -> None:
    """Remove a model's weights; ``WeightsInUse`` (nothing removed) while any deployment uses it."""
    path = canonical_path(org_id, model_ref)
    refs = await asyncio.to_thread(_references, db, org_id, model_ref)
    if refs:
        raise WeightsInUse(refs)
    await asyncio.to_thread(shutil.rmtree, path, True)
