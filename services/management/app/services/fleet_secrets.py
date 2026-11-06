"""Fleet secrets: per-deployment bearer tokens and cluster key rotation (AM11, AM13, AM15, AM16).

Management mints a 256-bit bearer token per deployment, writes it into the shared Kubernetes
Secret ``waddleai-vllm-deployment-keys`` (data key = deployment slug, mounted read-only into every
Ray worker so the Serve middleware re-reads it without a redeploy) and stores it encrypted in the
deployment's ``connection_links.api_key``. This module also lists and rotates the cluster-level
keys (Ray auth token, Ray internal TLS). A secret VALUE is never returned, logged, or placed in an
exception message: callers only ever see a 12-hex fingerprint.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import string
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# Kubernetes Secret OBJECT NAMES (not secret values); bandit B105 flags the word "auth"/"keys".
DEPLOYMENT_KEYS_SECRET = "waddleai-vllm-deployment-keys"  # noqa: S105  # nosec B105
RAY_AUTH_SECRET = "waddleai-ray-auth"  # noqa: S105  # nosec B105
RAY_TLS_SECRET = "waddleai-ray-tls"  # noqa: S105  # nosec B105
RESTART_ANNOTATION = "waddleai.penguintech.io/restart-generation"
_TLS_REISSUE_TIMEOUT_S = 60.0  # bounded wait for cert-manager to reissue the deleted TLS Secret
_TLS_REISSUE_POLL_S = 1.0
_RAY_TOKEN_ALPHABET = string.ascii_letters + string.digits  # Helm randAlphaNum alphabet
_RAY_TOKEN_LENGTH = 48


class SecretWriteFailed(RuntimeError):  # noqa: N818 -- public name fixed by the plan
    """A Kubernetes Secret write failed; the message never contains a secret value.

    ``partially_applied`` is True when an earlier step already took effect (the Secret rotated
    but the pod restart did not happen), so callers can say "rotated, restart pending, retry"
    instead of implying nothing changed.
    """

    def __init__(self, message: str = "secret write failed", *, partially_applied: bool = False):
        """Record the (value-free) message and whether an earlier step already took effect."""
        super().__init__(message)
        self.partially_applied = partially_applied


class UnknownSecret(KeyError):  # noqa: N818 -- public name fixed by the plan
    """The requested cluster secret name is not a rotatable fleet secret."""


class ManagedExternally(RuntimeError):  # noqa: N818 -- public name fixed by the plan
    """The secret is supplied by env or an existingSecret, so WaddleAI must not write it."""


class K8sGateway(Protocol):
    """The Kubernetes operations the fleet needs; sync, called via ``asyncio.to_thread``."""

    def read_secret(self, namespace: str, name: str) -> dict[str, str] | None:
        """Decoded ``data`` of a Secret, or None when it does not exist."""
        ...

    def patch_secret(self, namespace: str, name: str, data: dict[str, str | None]) -> None:
        """Merge-patch ``data`` keys (a ``None`` value removes the key)."""
        ...

    def delete_secret(self, namespace: str, name: str) -> None:
        """Delete a Secret (no error when absent)."""
        ...

    def create_secret(self, namespace: str, name: str, data: dict[str, str]) -> None:
        """Create a Secret with the given plain-text ``data`` (the gateway encodes it)."""
        ...

    def bump_restart_generation(self, namespace: str, kind: str, name: str) -> int:
        """Increment the pod-template restart-generation annotation; return the new value."""
        ...

    def create_job(self, namespace: str, manifest: dict[str, object]) -> None:
        """Create a batch Job from a manifest."""
        ...

    def job_state(self, namespace: str, name: str) -> str:
        """``"running"`` | ``"succeeded"`` | ``"failed"`` | ``"missing"`` for a Job."""
        ...

    def delete_job(self, namespace: str, name: str) -> None:
        """Delete a Job (and its pods)."""
        ...


@dataclass(slots=True, frozen=True)
class SecretInfo:
    """Metadata of a rotatable cluster secret; never its value."""

    name: str
    source: str  # "generated" | "external"
    fingerprint: str | None


@dataclass(slots=True, frozen=True)
class _SecretSpec:
    """Where a rotatable cluster secret lives and how it can be supplied externally."""

    secret: str
    key: str
    external_env: tuple[str, ...]


_SPECS: dict[str, _SecretSpec] = {
    "ray-auth-token": _SecretSpec(
        RAY_AUTH_SECRET,
        os.environ.get("WADDLEAI_RAY_AUTH_SECRET_KEY", "token"),
        ("WADDLEAI_RAY_AUTH_TOKEN", "WADDLEAI_RAY_AUTH_TOKEN_EXISTING_SECRET"),
    ),
    "ray-internal-tls": _SecretSpec(
        RAY_TLS_SECRET,
        "tls.crt",
        (
            "WADDLEAI_RAY_TLS_CERT_PATH",
            "WADDLEAI_RAY_TLS_KEY_PATH",
            "WADDLEAI_RAY_TLS_CA_PATH",
            "WADDLEAI_RAY_TLS_EXISTING_SECRET",
        ),
    ),
}
SECRET_NAMES = tuple(_SPECS)

_gateway: K8sGateway | None = None


def namespace() -> str:
    """Namespace holding the fleet Secrets (chart-provided; defaults to ``waddleai``)."""
    return os.environ.get("WADDLEAI_FLEET_NAMESPACE", "waddleai")


def ray_cluster_name() -> str:
    """Name of the KubeRay RayCluster whose pod templates get the restart annotation."""
    return os.environ.get("WADDLEAI_RAY_CLUSTER_NAME", "waddleai-vllm-ray")


def set_gateway(gateway: K8sGateway | None) -> None:
    """Install (or clear) the process-wide Kubernetes gateway; used by tests and startup."""
    global _gateway
    _gateway = gateway


def get_gateway() -> K8sGateway:
    """Return the installed gateway, else the in-cluster/kubeconfig-backed default."""
    global _gateway
    if _gateway is None:
        _gateway = KubernetesGateway()
    return _gateway


def mint_token() -> str:
    """A fresh 256-bit URL-safe bearer token."""
    return secrets.token_urlsafe(32)


def fingerprint(value: str) -> str:
    """First 12 hex chars of the SHA-256 of ``value``: identifies a key without revealing it."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _non_empty_env(name: str) -> bool:
    """True when env var ``name`` is set to a non-blank value."""
    return bool(os.environ.get(name, "").strip())


def secret_source(name: str) -> str:
    """``"external"`` when an AM15 env var or existingSecret supplies the key, else generated."""
    spec = _SPECS.get(name)
    if spec is None:
        raise UnknownSecret(name)
    return "external" if any(_non_empty_env(var) for var in spec.external_env) else "generated"


async def write_deployment_key(slug: str, token: str) -> None:
    """Merge-patch ``data.<slug>`` of the shared deployment-keys Secret (only that key)."""
    try:
        await asyncio.to_thread(
            get_gateway().patch_secret, namespace(), DEPLOYMENT_KEYS_SECRET, {slug: token}
        )
    except Exception:
        # never chain: the underlying client error may echo the request body (the token)
        logger.error("deployment key write failed for slug %s", slug)
        raise SecretWriteFailed("secret write failed") from None


async def read_deployment_key(slug: str) -> str | None:
    """Current ``data.<slug>`` of the shared deployment-keys Secret, or None when absent."""
    try:
        data = await asyncio.to_thread(
            get_gateway().read_secret, namespace(), DEPLOYMENT_KEYS_SECRET
        )
    except Exception:
        logger.error("deployment key read failed for slug %s", slug)
        raise SecretWriteFailed("secret read failed") from None
    return (data or {}).get(slug)


async def restore_deployment_key(slug: str, previous: str | None) -> None:
    """Put ``data.<slug>`` back to ``previous`` (or remove it when there was none)."""
    if previous is None:
        await remove_key(slug)
    else:
        await write_deployment_key(slug, previous)


async def remove_hf_token_secret(deployment_id: int) -> None:
    """Delete the short-lived ``waddleai-hf-<id>`` pull-token Secret (absent is fine)."""
    try:
        await asyncio.to_thread(
            get_gateway().delete_secret, namespace(), f"waddleai-hf-{deployment_id}"
        )
    except Exception:
        logger.error("hf token secret removal failed for deployment %s", deployment_id)
        raise SecretWriteFailed("secret write failed") from None


async def remove_key(slug: str) -> None:
    """Remove ``data.<slug>`` from the shared deployment-keys Secret (merge-patch to null)."""
    try:
        await asyncio.to_thread(
            get_gateway().patch_secret, namespace(), DEPLOYMENT_KEYS_SECRET, {slug: None}
        )
    except Exception:
        logger.error("deployment key removal failed for slug %s", slug)
        raise SecretWriteFailed("secret write failed") from None


async def provision_key_and_link(
    database: Any,
    *,
    org_id: int | None,
    slug: str,
    link_name: str,
    models: list[str],
    provider: str = "vllm",
) -> tuple[str, int]:
    """Mint a deployment token, publish it to the shared Secret and store it encrypted.

    The connection link is created DISABLED: it holds the encrypted token (the readiness canary
    and the proxy read it) and is enabled by the reconciler once the deployment serves. Raises
    ``SecretWriteFailed`` before anything is written to the database.
    """
    from shared.database.models import insert_connection_link

    token = mint_token()
    await write_deployment_key(slug, token)

    def _insert() -> int:
        link_id = insert_connection_link(
            database,
            api_key=token,
            name=link_name,
            provider=provider,
            endpoint_url="",
            model_list=models,
            enabled=False,
            owner_org_id=org_id,
        )
        database.commit()
        return int(link_id)

    return token, await asyncio.to_thread(_insert)


async def bump_restart(kind: str, name: str) -> int:
    """Bump the restart-generation annotation so KubeRay / the StatefulSet recreate pods."""
    try:
        return await asyncio.to_thread(
            get_gateway().bump_restart_generation, namespace(), kind, name
        )
    except Exception:
        logger.error("restart bump failed for %s/%s", kind, name)
        raise SecretWriteFailed("restart failed") from None


async def list_secrets() -> list[SecretInfo]:
    """Rotatable cluster secrets with source and fingerprint (never the value)."""
    infos: list[SecretInfo] = []
    for name, spec in _SPECS.items():
        data = await asyncio.to_thread(get_gateway().read_secret, namespace(), spec.secret)
        value = (data or {}).get(spec.key)
        infos.append(SecretInfo(name, secret_source(name), fingerprint(value) if value else None))
    return infos


async def rotate_secret(name: str) -> SecretInfo:
    """Rotate a generated cluster secret and roll the Ray pods; refuses external keys.

    ``ray-auth-token`` gets a new 48-char alphanumeric token; ``ray-internal-tls`` has its Secret
    deleted so cert-manager reissues it. Either way the RayCluster restart-generation annotation
    is bumped so head and workers pick the new material up.
    """
    spec = _SPECS.get(name)
    if spec is None:
        raise UnknownSecret(name)
    if secret_source(name) == "external":
        raise ManagedExternally(name)
    gateway = get_gateway()
    try:
        if name == "ray-auth-token":
            token = "".join(secrets.choice(_RAY_TOKEN_ALPHABET) for _ in range(_RAY_TOKEN_LENGTH))
            await asyncio.to_thread(
                gateway.patch_secret, namespace(), spec.secret, {spec.key: token}
            )
        else:
            await asyncio.to_thread(gateway.delete_secret, namespace(), spec.secret)
    except Exception:
        logger.error("cluster secret rotation failed for %s", name)
        raise SecretWriteFailed("secret write failed") from None
    if name == "ray-internal-tls":
        await _wait_for_reissue(gateway, spec)
    try:
        await bump_restart("RayCluster", ray_cluster_name())
    except SecretWriteFailed:
        # the Secret already rotated: tell the caller the restart is what is still pending
        raise SecretWriteFailed("restart failed", partially_applied=True) from None
    data = await asyncio.to_thread(gateway.read_secret, namespace(), spec.secret)
    value = (data or {}).get(spec.key)
    return SecretInfo(name, "generated", fingerprint(value) if value else None)


async def _wait_for_reissue(gateway: K8sGateway, spec: _SecretSpec) -> None:
    """Wait (bounded) until the deleted TLS Secret is readable again; fail loudly on timeout.

    Restarting before cert-manager reissues the Secret would roll the Ray pods onto a missing
    mount, so the restart bump is only issued once the new material exists.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _TLS_REISSUE_TIMEOUT_S
    while True:
        try:
            data = await asyncio.to_thread(gateway.read_secret, namespace(), spec.secret)
        except Exception:
            logger.error("ray internal tls reissue poll failed")
            raise SecretWriteFailed("secret read failed", partially_applied=True) from None
        if (data or {}).get(spec.key):
            return
        if loop.time() >= deadline:
            logger.error("ray internal tls not reissued within %ss", _TLS_REISSUE_TIMEOUT_S)
            raise SecretWriteFailed("tls reissue timed out", partially_applied=True)
        await asyncio.sleep(_TLS_REISSUE_POLL_S)


class KubernetesGateway:
    """``K8sGateway`` over the official ``kubernetes`` client (in-cluster, else kubeconfig)."""

    def __init__(self) -> None:
        """Load cluster credentials lazily on first use."""
        self._core: object | None = None
        self._apps: object | None = None
        self._batch: object | None = None
        self._custom: object | None = None

    def _clients(self) -> tuple[object, object, object, object]:
        """Build (once) the Core, Apps, Batch and CustomObjects API clients."""
        if self._core is None:
            from kubernetes import client
            from kubernetes import config as k8s_config

            try:
                k8s_config.load_incluster_config()
            except Exception:
                logger.debug("not in-cluster; falling back to kubeconfig")
                k8s_config.load_kube_config()
            self._core = client.CoreV1Api()
            self._apps = client.AppsV1Api()
            self._batch = client.BatchV1Api()
            self._custom = client.CustomObjectsApi()
        return self._core, self._apps, self._batch, self._custom  # type: ignore[return-value]

    def read_secret(self, namespace: str, name: str) -> dict[str, str] | None:
        """Decoded ``data`` of a Secret, or None when absent."""
        import base64

        from kubernetes.client.exceptions import ApiException

        core: Any = self._clients()[0]
        try:
            secret = core.read_namespaced_secret(name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
        return {k: base64.b64decode(v).decode() for k, v in (secret.data or {}).items()}

    def patch_secret(self, namespace: str, name: str, data: dict[str, str | None]) -> None:
        """Merge-patch Secret data (base64-encoding values; ``None`` deletes the key)."""
        import base64

        core: Any = self._clients()[0]
        body = {
            "data": {
                k: (None if v is None else base64.b64encode(v.encode()).decode())
                for k, v in data.items()
            }
        }
        core.patch_namespaced_secret(name, namespace, body)

    def delete_secret(self, namespace: str, name: str) -> None:
        """Delete a Secret; an already-absent Secret is not an error."""
        from kubernetes.client.exceptions import ApiException

        core: Any = self._clients()[0]
        try:
            core.delete_namespaced_secret(name, namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise

    def create_secret(self, namespace: str, name: str, data: dict[str, str]) -> None:
        """Create an Opaque Secret from plain ``string_data``."""
        from kubernetes import client

        core: Any = self._clients()[0]
        core.create_namespaced_secret(
            namespace,
            client.V1Secret(metadata=client.V1ObjectMeta(name=name), string_data=data),
        )

    def bump_restart_generation(self, namespace: str, kind: str, name: str) -> int:
        """Increment the pod-template restart-generation annotation (head and workers)."""
        _core, apps, _batch, custom = self._clients()
        if kind == "RayCluster":
            return self._bump_raycluster(custom, namespace, name)
        if kind in ("StatefulSet", "Deployment"):
            return self._bump_workload(apps, namespace, kind, name)
        raise ValueError(f"unsupported restart target kind {kind}")

    @staticmethod
    def _bump_workload(apps: object, namespace: str, kind: str, name: str) -> int:
        """Increment the restart annotation on a StatefulSet or Deployment pod template."""
        api: Any = apps
        read = (
            api.read_namespaced_stateful_set
            if kind == "StatefulSet"
            else api.read_namespaced_deployment
        )
        patch = (
            api.patch_namespaced_stateful_set
            if kind == "StatefulSet"
            else api.patch_namespaced_deployment
        )
        workload = read(name, namespace)
        annotations = dict(workload.spec.template.metadata.annotations or {})
        generation = int(annotations.get(RESTART_ANNOTATION, "0")) + 1
        patch(
            name,
            namespace,
            {
                "spec": {
                    "template": {"metadata": {"annotations": {RESTART_ANNOTATION: str(generation)}}}
                }
            },
        )
        return generation

    @staticmethod
    def _bump_raycluster(custom: object, namespace: str, name: str) -> int:
        """Bump the annotation on the RayCluster head and every worker group template."""
        api: Any = custom
        coords = {
            "group": "ray.io",
            "version": "v1",
            "namespace": namespace,
            "plural": "rayclusters",
            "name": name,
        }
        cluster = api.get_namespaced_custom_object(**coords)
        spec = cluster["spec"]
        current = int(
            (
                (spec["headGroupSpec"]["template"].get("metadata") or {}).get("annotations") or {}
            ).get(RESTART_ANNOTATION, "0")
        )
        generation = current + 1
        value = {"metadata": {"annotations": {RESTART_ANNOTATION: str(generation)}}}
        patch = {
            "spec": {
                "headGroupSpec": {"template": value},
                "workerGroupSpecs": [
                    {"groupName": group["groupName"], "template": value}
                    for group in spec.get("workerGroupSpecs", [])
                ],
            }
        }
        api.patch_namespaced_custom_object(body=patch, **coords)
        return generation

    def create_job(self, namespace: str, manifest: dict[str, object]) -> None:
        """Create a batch Job."""
        from kubernetes.client.exceptions import ApiException

        batch: Any = self._clients()[2]
        try:
            batch.create_namespaced_job(namespace, manifest)
        except ApiException as exc:
            if exc.status != 409:  # one pull Job per (cluster, model): an existing one is fine
                raise

    def job_state(self, namespace: str, name: str) -> str:
        """Job state from its status counters; ``missing`` when the Job does not exist."""
        from kubernetes.client.exceptions import ApiException

        batch: Any = self._clients()[2]
        try:
            job = batch.read_namespaced_job(name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                return "missing"
            raise
        if job.status.succeeded:
            return "succeeded"
        return "failed" if job.status.failed else "running"

    def delete_job(self, namespace: str, name: str) -> None:
        """Delete a Job and its pods (background propagation)."""
        from kubernetes.client.exceptions import ApiException

        batch: Any = self._clients()[2]
        try:
            batch.delete_namespaced_job(name, namespace, propagation_policy="Background")
        except ApiException as exc:
            if exc.status != 404:
                raise
