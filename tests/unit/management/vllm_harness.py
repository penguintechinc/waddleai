"""Shared fakes for the vLLM fleet API tests: a real sqlite penguin-dal DB and a fake Kubernetes.

The mocked management DB ignores query predicates, so these tests run the real routes against a
genuine sqlite-backed penguin-dal ``DB`` built from the ORM metadata (every table the fleet
touches) plus the PyDAL-only ``connection_links`` table. Filters, org scoping, pagination and
ordering are therefore exercised for real.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from penguin_dal import DAL, Field
from sqlalchemy import DefaultClause, create_engine, text

from services.management.app.models_sqlalchemy import Base
from services.management.app.services.fleet_secrets import DEPLOYMENT_KEYS_SECRET


def _mirror_python_defaults() -> None:
    """Give NOT NULL columns their scalar Python default as a server default.

    Production schemas come from migrations (which carry server defaults); ``create_all`` on
    sqlite only has the ORM's Python-side defaults, which penguin-dal inserts never apply.
    """
    for table in Base.metadata.tables.values():
        for column in table.columns:
            default = column.default
            if (
                default is not None
                and getattr(default, "is_scalar", False)
                and column.server_default is None
                and not column.nullable
            ):
                value = default.arg
                literal = (
                    str(int(value))
                    if isinstance(value, bool)
                    else repr(value)
                    if isinstance(value, (int, float))
                    else "'" + str(value).replace("'", "''") + "'"
                )
                column.server_default = DefaultClause(text(literal))


def build_db(tmp_path: Path) -> Any:
    """A sqlite penguin-dal DB with the ORM schema and the PyDAL ``connection_links`` table."""
    url = f"sqlite:///{tmp_path / 'fleet.db'}"
    _mirror_python_defaults()
    Base.metadata.create_all(create_engine(url))
    db = DAL(url)
    db.define_table(
        "connection_links",
        Field("name", unique=True, notnull=True),
        Field("provider", "string", notnull=True),
        Field("endpoint_url", notnull=True),
        Field("api_key", "password"),
        Field("model_list", "json"),
        Field("rate_limits", "json"),
        Field("enabled", "boolean", default=True),
        Field("tls_config", "json"),
        Field("management_capabilities", "json"),
        Field("owner_org_id", "integer"),
    )
    return db


NODE_COST = {
    "cpu_usd": 1000,
    "ram_gb": 128,
    "nvme_gb": 2000,
    "nvme_pro": True,
    "network_devices": 3,
    "node_base_watts": 300,
    "gpus_per_node": 4,
}
BACKEND_CONFIG: dict[str, Any] = {
    "head_url": "http://head:8265",
    "serve_base_url": "http://serve:8000",
    "tco": {"kwh_usd": 0.18},
    "worker_groups": [
        {"gpu_class": "nvidia-rtx4090", "cost": {"est_tokens_per_s": 40.0, "node": NODE_COST}},
        {"gpu_class": "nvidia-a100-80g", "cost": {"est_tokens_per_s": 90.0, "node": NODE_COST}},
    ],
}


def seed_backend(db: Any, org_id: int = 1, name: str = "gpu", config: dict | None = None) -> int:
    """Insert a vllm_ray fleet backend; returns its id."""
    now = datetime.utcnow()
    backend_id = db.fleet_backends.insert(
        org_id=org_id,
        name=name,
        type="vllm_ray",
        management_scope="full_lifecycle",
        config=config if config is not None else BACKEND_CONFIG,
        status="active",
        created_at=now,
        updated_at=now,
    )
    db.commit()
    return int(backend_id)


def seed_gpus(
    db: Any,
    backend_id: int,
    org_id: int = 1,
    *,
    node: str = "node-a",
    gpu_class: str | None = "nvidia-rtx4090",
    count: int = 4,
    vram_mb: int = 24564,
    state: int = 1,
    schedulable: bool = True,
    vendor: int = 1,
) -> None:
    """Insert ``count`` GPUs on one node."""
    for index in range(count):
        db.gpu_inventory.insert(
            org_id=org_id,
            fleet_backend_id=backend_id,
            node_uid=f"uid-{node}",
            node_name=node,
            gpu_index=index,
            vendor=vendor,
            gpu_class=gpu_class,
            product_name="GPU",
            vram_total_mb=vram_mb,
            vram_used_mb=0,
            pci_bus_id=f"0:{index}",
            schedulable=schedulable,
            state=state,
            last_seen=datetime.utcnow(),
        )
    db.commit()


def seed_rate(
    db: Any, provider: str = "openai", model: str = "gpt-4o", enabled: bool = True
) -> None:
    """Insert a commercial conversion-rate row (the savings baseline)."""
    db.token_conversion_rates.insert(
        provider=provider, model=model, input_rate=1000.0, output_rate=500.0, enabled=enabled
    )
    db.commit()


def seed_deployment(db: Any, backend_id: int, org_id: int = 1, **over: Any) -> Any:
    """Insert a vLLM deployment row and return it."""
    public_id = uuid.UUID(str(over.pop("public_id", uuid.uuid4())))
    name = over.pop("name", "dep")
    slug = over.pop("slug", f"{name}-{public_id.hex[:12]}")
    now = datetime.utcnow()
    values: dict[str, Any] = {
        "public_id": str(public_id),
        "org_id": org_id,
        "fleet_backend_id": backend_id,
        "name": name,
        "slug": slug,
        "model_ref": "org/Model-7B",
        "served_model_name": "model-7b",
        "vendor": 1,
        "gpu_class": "nvidia-rtx4090",
        "allow_mixed_class": False,
        "quantization": 0,
        "dtype": "bfloat16",
        "tensor_parallel": 1,
        "pipeline_parallel": 1,
        "replicas_min": 1,
        "replicas_max": 1,
        "max_model_len": 4096,
        "status": 5,
        "serve_app_name": f"d-{slug}",
        "endpoint_url": f"http://serve:8000/d/{slug}/v1",
        "expose_via_proxy": True,
        "comparison_model": "openai/gpt-4o",
        "price_mode": 1,
        "price_input_per_1m": "1.0",
        "price_output_per_1m": "5.0",
        "created_at": now,
        "updated_at": now,
    }
    values.update(over)
    dep_id = db.vllm_deployments.insert(**values)
    db.commit()
    return db(db.vllm_deployments.id == dep_id).select().first()


@dataclass(slots=True)
class FakeK8s:
    """In-memory Kubernetes gateway recording every Secret/Job/restart operation."""

    secrets: dict[str, dict[str, str]] = field(default_factory=dict)
    patches: list[tuple[str, dict[str, str | None]]] = field(default_factory=list)
    restart: dict[str, int] = field(default_factory=dict)
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    job_states: dict[str, str] = field(default_factory=dict)
    deleted_secrets: list[str] = field(default_factory=list)
    fail_next_patch: bool = False

    def read_secret(self, namespace: str, name: str) -> dict[str, str] | None:
        """Secret data or None."""
        data = self.secrets.get(name)
        return None if data is None else dict(data)

    def patch_secret(self, namespace: str, name: str, data: dict[str, str | None]) -> None:
        """Merge-patch; ``None`` removes a key; ``fail_next_patch`` raises once."""
        if self.fail_next_patch:
            self.fail_next_patch = False
            raise RuntimeError("apiserver unavailable")
        self.patches.append((name, dict(data)))
        target = self.secrets.setdefault(name, {})
        for key, value in data.items():
            if value is None:
                target.pop(key, None)
            else:
                target[key] = value

    def delete_secret(self, namespace: str, name: str) -> None:
        """Delete a Secret."""
        self.deleted_secrets.append(name)
        self.secrets.pop(name, None)

    def create_secret(self, namespace: str, name: str, data: dict[str, str]) -> None:
        """Create a Secret."""
        self.secrets[name] = dict(data)

    def bump_restart_generation(self, namespace: str, kind: str, name: str) -> int:
        """Increment the restart generation."""
        key = f"{kind}/{name}"
        self.restart[key] = self.restart.get(key, 0) + 1
        return self.restart[key]

    def create_job(self, namespace: str, manifest: dict[str, Any]) -> None:
        """Record a Job."""
        self.jobs[manifest["metadata"]["name"]] = manifest

    def job_state(self, namespace: str, name: str) -> str:
        """Scripted Job state (default running)."""
        return self.job_states.get(name, "running")

    def delete_job(self, namespace: str, name: str) -> None:
        """Delete a Job."""
        self.jobs.pop(name, None)

    def deployment_key(self, slug: str) -> str | None:
        """The token stored for ``slug`` in the shared deployment-keys Secret."""
        return self.secrets.get(DEPLOYMENT_KEYS_SECRET, {}).get(slug)


async def _ok_app(scope: dict, receive: Any, send: Any) -> None:
    """A trivial ASGI app answering 200."""
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"{}"})


class RunningEngine:
    """Fake vLLM (key re-read per request) and llama-server (key loaded at start) engines."""

    SLUG = "engine"

    def __init__(self, tmp_path: Path) -> None:
        """Create both key dirs and start with the token ``initial-token``."""
        self._dirs = {"vllm": tmp_path / "vllm-keys", "llamacpp": tmp_path / "llama-keys"}
        for directory in self._dirs.values():
            directory.mkdir()
        self.token = "initial-token"  # noqa: S105 -- test fixture
        self._llama_loaded = ""
        for kind in self._dirs:
            self.set_token(kind, self.token)
        self.restart("llamacpp")

    def set_token(self, kind: str, token: str) -> None:
        """Write the key file the engine reads."""
        (self._dirs[kind] / self.SLUG).write_text(token)

    def restart(self, kind: str) -> None:
        """llama-server loads ``--api-key-file`` once at startup: snapshot it now."""
        if kind == "llamacpp":
            self._llama_loaded = (self._dirs[kind] / self.SLUG).read_text()

    def client(self, kind: str) -> Any:
        """An httpx client bound to the engine's ASGI app."""
        import httpx

        from shared.fleet.vllm_ray.serve_app import BearerKeyMiddleware

        if kind == "vllm":
            app = BearerKeyMiddleware(_ok_app, self.SLUG, str(self._dirs["vllm"]))
        else:
            snapshot = self._dirs["llamacpp"] / "loaded"
            snapshot.mkdir(exist_ok=True)
            (snapshot / self.SLUG).write_text(self._llama_loaded)
            app = BearerKeyMiddleware(_ok_app, self.SLUG, str(snapshot))
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine")


class FakeBackend:
    """Records lifecycle calls; ``deploy_error`` / ``delete_error`` script failures."""

    def __init__(self) -> None:
        """Start with an empty call log and no scripted failure."""
        self.calls: list[tuple[str, Any]] = []
        self.deploy_error: str | None = None
        self.delete_error: str | None = None
        self.reconcile_results: dict[int, list[int]] = {}
        self.inventory_calls = 0

    async def deploy(self, dep: Any) -> None:
        """Record; optionally fail like Plan B (row becomes FAILED with the detail)."""
        self.calls.append(("deploy", dep.slug))
        if self.deploy_error:
            raise RuntimeError(self.deploy_error)

    async def scale(self, dep: Any, replicas_min: int, replicas_max: int) -> None:
        """Record the scale request."""
        self.calls.append(("scale", (replicas_min, replicas_max)))

    async def stop(self, dep: Any) -> None:
        """Record the stop request."""
        self.calls.append(("stop", dep.slug))

    async def delete(self, dep: Any) -> None:
        """Record the delete request; optionally fail."""
        self.calls.append(("delete", dep.slug))
        if self.delete_error:
            raise RuntimeError(self.delete_error)

    async def reconcile(self, dep: Any) -> int:
        """Return the next scripted status for the deployment (else its current status)."""
        self.calls.append(("reconcile", dep.id))
        script = self.reconcile_results.get(dep.id)
        if script:
            dep.status = script.pop(0)
        return int(dep.status)

    async def collect_inventory(self) -> list[Any]:
        """Count inventory refreshes."""
        self.inventory_calls += 1
        return []
