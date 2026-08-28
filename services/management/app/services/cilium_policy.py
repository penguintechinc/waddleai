"""Cilium Policy Reconciler.

Management is the control-plane authority for Cilium data-plane policy
(platform spec §12, §5.3): this module renders WaddleAI's own Postgres-backed
settings into Cilium CRDs (``CiliumEnvoyConfig`` for per-org edge rate
limiting, ``CiliumNetworkPolicy``/``CiliumClusterwideNetworkPolicy`` for
network isolation) and upserts them via the Kubernetes API, so enforcement
happens at the Cilium/Envoy edge instead of inside Management's or AIProxy's
request path.

Render functions (``render_envoy_config``, ``render_network_policies``) are
pure, deterministic, and cluster-free — no I/O, no k8s client. The
``CiliumPolicyReconciler`` orchestrates capability detection, DB loading, and
a create-or-replace upsert; it never raises into a caller. Any failure path
(feature flag off, CRDs absent, k8s API unreachable, mid-upsert error)
degrades to a ``ReconcileStatus`` instead of propagating, per §12.3's
CRD-absent graceful-degradation requirement.

Per-org rate limiting only (spec §5.3/Q#10) — per-key limits stay in the
AIProxy token gate (``shared/utils/token_limiter.py``) to avoid CEC churn on
key rotation.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

CILIUM_GROUP = "cilium.io"
CILIUM_VERSION = "v2"
CNP_PLURAL = "ciliumnetworkpolicies"
CEC_PLURAL = "ciliumenvoyconfigs"
CCNP_PLURAL = "ciliumclusterwidenetworkpolicies"

_ORG_HEADER = "x-waddleai-org-id"
_CEC_NAME = "waddleai-org-ratelimit"
_NATIVE_RATE_LIMIT_FLAG = "waddleai.native_rate_limit"

# Default topology — mirrors the JSON emitted by the Helm
# `waddleai.cilium.topology` helper (k8s/helm/waddleai/templates/_helpers.tpl)
# and consumed via the CILIUM_TOPOLOGY env var. Used whenever that env var is
# absent or malformed (e.g. local dev, unit tests).
DEFAULT_TOPOLOGY: dict[str, Any] = {
    "namespace": "waddleai",
    "gateway_name": "shared",
    "gateway_namespace": "gateway",
    "aiproxy_port": 8080,
    "postgres_port": 5432,
    "valkey_port": 6379,
    "fleet_ports": [8080, 11434],
    "fleet_component_key": "app.kubernetes.io/component",
    "fleet_components": ["ollama", "llamacpp"],
    "selectors": {
        "gateway": {"app.kubernetes.io/name": "cilium-gateway"},
        "aiproxy": {
            "app.kubernetes.io/name": "waddleai",
            "app.kubernetes.io/component": "proxy",
        },
        "management": {
            "app.kubernetes.io/name": "waddleai",
            "app.kubernetes.io/component": "management",
        },
        "postgres": {
            "app.kubernetes.io/name": "waddleai",
            "app.kubernetes.io/component": "postgres",
        },
        "valkey": {
            "app.kubernetes.io/name": "waddleai",
            "app.kubernetes.io/component": "valkey",
        },
    },
}

# NOTE (deviation from the phase-1 plan, tracked deliberately rather than
# silently): the plan calls for an `organizations.rpm_limit` column added by
# *this* branch. Platform spec §13.1 assigns that exact column to Alembic
# migration 007 (`drop_ailb_add_native_limits`), owned by a different branch
# in the dependency chain. Per house rule, this branch does not create or
# touch Alembic migrations. `_load_orgs()` below reads `rpm_limit`
# defensively via `getattr`/`hasattr` against whatever penguin-dal reflects
# from the live schema, so the per-org edge limit activates automatically
# once migration 007 lands elsewhere — no further change needed here.


# ---------------------------------------------------------------------------
# Kubernetes client loaders (in-cluster -> kubeconfig fallback, mirrors
# services/management/app/services/llamacpp_manager.py)
# ---------------------------------------------------------------------------


def get_k8s_apiext_client() -> Any:
    """Return a configured ApiextensionsV1Api client (in-cluster, else kubeconfig)."""
    from kubernetes import client  # type: ignore[import]
    from kubernetes import config as k8s_config

    try:
        k8s_config.load_incluster_config()
    except Exception:
        logger.debug("Not running in-cluster, falling back to kubeconfig")
        k8s_config.load_kube_config()
    return client.ApiextensionsV1Api()


def get_k8s_custom_objects_client() -> Any:
    """Return a configured CustomObjectsApi client (in-cluster, else kubeconfig)."""
    from kubernetes import client  # type: ignore[import]
    from kubernetes import config as k8s_config

    try:
        k8s_config.load_incluster_config()
    except Exception:
        logger.debug("Not running in-cluster, falling back to kubeconfig")
        k8s_config.load_kube_config()
    return client.CustomObjectsApi()


def cilium_capabilities() -> dict[str, bool]:
    """Detect which Cilium CRDs are installed on the cluster.

    Never raises: any client construction or API failure (no kubeconfig, 403,
    connection refused, cluster has no Cilium CRDs at all, ...) degrades to
    "nothing available" so callers can no-op cleanly on non-Cilium clusters
    (§12.3).
    """
    result = {"network_policy": False, "envoy_config": False, "available": False}
    try:
        apiext = get_k8s_apiext_client()
        crds = apiext.list_custom_resource_definition()
        names = {crd.metadata.name for crd in crds.items}
    except Exception as exc:
        logger.warning("Cilium CRD capability detection failed, treating as absent: %s", exc)
        return result

    result["network_policy"] = f"{CNP_PLURAL}.{CILIUM_GROUP}" in names
    result["envoy_config"] = f"{CEC_PLURAL}.{CILIUM_GROUP}" in names
    result["available"] = result["network_policy"] or result["envoy_config"]
    return result


def is_native_rate_limit_enabled() -> bool:
    """Fail-safe-OFF check of the ``waddleai.native_rate_limit`` PostHog flag.

    Imports the feature-flag helper defensively: if `shared.utils.feature_flags`
    is unavailable for any reason, the reconciler must still fail safe to OFF
    rather than raise (§14.5).
    """
    try:
        from shared.utils.feature_flags import is_feature_enabled

        return is_feature_enabled(_NATIVE_RATE_LIMIT_FLAG, distinct_id="_global", default=False)
    except Exception as exc:
        logger.warning(
            "Feature flag evaluation failed, treating %s as OFF: %s", _NATIVE_RATE_LIMIT_FLAG, exc
        )
        return False


# ---------------------------------------------------------------------------
# Pure render functions — no k8s calls, no DB calls, deterministic output
# ---------------------------------------------------------------------------


def render_envoy_config(
    orgs: list[tuple[int, str, int | None, bool]],
    topology: dict[str, Any],
) -> dict[str, Any]:
    """Render the per-org CiliumEnvoyConfig ``local_ratelimit`` descriptor set.

    ``orgs`` is a list of ``(org_id, name, rpm_limit, enabled)`` tuples. Orgs
    with ``rpm_limit is None`` (no edge limit configured) or ``enabled=False``
    are excluded — they still get per-key gating downstream in the AIProxy
    token gate (spec §5.3/Q#10), just no edge-level limit. Sorted by org_id
    for deterministic, snapshot-stable output.
    """
    namespace = topology["namespace"]
    gateway_name = topology.get("gateway_name", "shared")
    gateway_namespace = topology.get("gateway_namespace", namespace)

    descriptors = []
    for org_id, _name, rpm_limit, enabled in sorted(orgs, key=lambda o: o[0]):
        if not enabled or rpm_limit is None:
            continue
        descriptors.append(
            {
                "entries": [{"key": _ORG_HEADER, "value": str(org_id)}],
                "token_bucket": {
                    "max_tokens": rpm_limit,
                    "tokens_per_fill": rpm_limit,
                    "fill_interval": "60s",
                },
            }
        )

    return {
        "apiVersion": f"{CILIUM_GROUP}/{CILIUM_VERSION}",
        "kind": "CiliumEnvoyConfig",
        "metadata": {"name": _CEC_NAME, "namespace": namespace},
        "spec": {
            "services": [{"name": gateway_name, "namespace": gateway_namespace}],
            "resources": [
                {
                    "@type": (  # noqa: E501 -- Envoy typed_config URL, cannot be shortened
                        "type.googleapis.com/envoy.extensions.filters.http.local_ratelimit.v3.LocalRateLimit"
                    ),
                    "name": "waddleai-org-ratelimit",
                    "stat_prefix": "waddleai_org_ratelimit",
                    "descriptors": descriptors,
                }
            ],
        },
    }


def _namespaced_labels(selector: dict[str, str], namespace: str | None = None) -> dict[str, Any]:
    """MatchLabels dict for `selector`, optionally scoped to a foreign namespace."""
    labels = dict(selector)
    if namespace:
        labels["k8s:io.kubernetes.pod.namespace"] = namespace
    return {"matchLabels": labels}


def _fleet_selector(topology: dict[str, Any]) -> dict[str, Any]:
    """EndpointSelector matching every fleet component (ollama, llamacpp, ...)."""
    key = topology.get("fleet_component_key", "app.kubernetes.io/component")
    values = topology.get("fleet_components", ["ollama", "llamacpp"])
    return {"matchExpressions": [{"key": key, "operator": "In", "values": list(values)}]}


def _to_ports(ports: list[int]) -> list[dict[str, Any]]:
    return [{"ports": [{"port": str(p), "protocol": "TCP"} for p in ports]}]


def render_network_policies(topology: dict[str, Any]) -> list[dict[str, Any]]:
    """Render the default-deny + explicit-flow CiliumNetworkPolicy set (§12.1).

    Flows rendered (spec §12.1, §10.3):
      - default-deny per namespace
      - client -> Gateway (CiliumClusterwideNetworkPolicy: Gateway pods live
        outside this chart's own namespace, so this is cluster-scoped rather
        than namespaced — see judgment-call note below)
      - Gateway -> AIProxy
      - AIProxy -> fleet / Postgres / Valkey (egress)
      - fleet admits ingress ONLY from AIProxy (§10.3)
      - Postgres / Valkey admit ingress from AIProxy + Management
      - Management -> Postgres / Valkey / kube-apiserver (egress)

    Judgment call: the Gateway itself is not a resource this chart deploys
    (topology.gateway_namespace is a foreign namespace owned by whatever
    installs Cilium Gateway API). A namespaced CiliumNetworkPolicy cannot
    select pods outside its own namespace, so "client -> Gateway" is rendered
    as a CiliumClusterwideNetworkPolicy instead. It is intentionally
    permissive (fromEntities: [world, cluster]) — real internet-facing
    ingress control belongs to the Gateway's own chart; this policy exists so
    §12.1's flow list has a single, complete, toggleable source of truth.
    """
    namespace = topology["namespace"]
    gateway_namespace = topology.get("gateway_namespace", namespace)
    selectors = topology.get("selectors", {})
    gateway_sel = selectors.get("gateway", {})
    aiproxy_sel = selectors.get("aiproxy", {})
    management_sel = selectors.get("management", {})
    postgres_sel = selectors.get("postgres", {})
    valkey_sel = selectors.get("valkey", {})
    fleet_sel = _fleet_selector(topology)

    aiproxy_port = topology.get("aiproxy_port", 8080)
    postgres_port = topology.get("postgres_port", 5432)
    valkey_port = topology.get("valkey_port", 6379)
    fleet_ports = topology.get("fleet_ports", [8080, 11434])

    policies: list[dict[str, Any]] = []

    policies.append(
        {
            "apiVersion": f"{CILIUM_GROUP}/{CILIUM_VERSION}",
            "kind": "CiliumNetworkPolicy",
            "metadata": {"name": "waddleai-default-deny", "namespace": namespace},
            "spec": {"endpointSelector": {}, "ingress": [], "egress": []},
        }
    )

    policies.append(
        {
            "apiVersion": f"{CILIUM_GROUP}/{CILIUM_VERSION}",
            "kind": "CiliumClusterwideNetworkPolicy",
            "metadata": {"name": f"{namespace}-allow-client-to-gateway"},
            "spec": {
                "endpointSelector": _namespaced_labels(gateway_sel, gateway_namespace),
                "ingress": [{"fromEntities": ["world", "cluster"]}],
            },
        }
    )

    policies.append(
        {
            "apiVersion": f"{CILIUM_GROUP}/{CILIUM_VERSION}",
            "kind": "CiliumNetworkPolicy",
            "metadata": {"name": "waddleai-allow-gateway-to-aiproxy", "namespace": namespace},
            "spec": {
                "endpointSelector": _namespaced_labels(aiproxy_sel),
                "ingress": [
                    {
                        "fromEndpoints": [_namespaced_labels(gateway_sel, gateway_namespace)],
                        "toPorts": _to_ports([aiproxy_port]),
                    }
                ],
            },
        }
    )

    policies.append(
        {
            "apiVersion": f"{CILIUM_GROUP}/{CILIUM_VERSION}",
            "kind": "CiliumNetworkPolicy",
            "metadata": {"name": "waddleai-allow-aiproxy-egress", "namespace": namespace},
            "spec": {
                "endpointSelector": _namespaced_labels(aiproxy_sel),
                "egress": [
                    {"toEndpoints": [fleet_sel], "toPorts": _to_ports(fleet_ports)},
                    {
                        "toEndpoints": [_namespaced_labels(postgres_sel)],
                        "toPorts": _to_ports([postgres_port]),
                    },
                    {
                        "toEndpoints": [_namespaced_labels(valkey_sel)],
                        "toPorts": _to_ports([valkey_port]),
                    },
                ],
            },
        }
    )

    # §10.3: fleet pods admit ingress exclusively from AIProxy — exactly one
    # fromEndpoints entry, no other source.
    policies.append(
        {
            "apiVersion": f"{CILIUM_GROUP}/{CILIUM_VERSION}",
            "kind": "CiliumNetworkPolicy",
            "metadata": {"name": "waddleai-allow-fleet-ingress", "namespace": namespace},
            "spec": {
                "endpointSelector": fleet_sel,
                "ingress": [
                    {
                        "fromEndpoints": [_namespaced_labels(aiproxy_sel)],
                        "toPorts": _to_ports(fleet_ports),
                    }
                ],
            },
        }
    )

    policies.append(
        {
            "apiVersion": f"{CILIUM_GROUP}/{CILIUM_VERSION}",
            "kind": "CiliumNetworkPolicy",
            "metadata": {"name": "waddleai-allow-postgres-ingress", "namespace": namespace},
            "spec": {
                "endpointSelector": _namespaced_labels(postgres_sel),
                "ingress": [
                    {
                        "fromEndpoints": [
                            _namespaced_labels(aiproxy_sel),
                            _namespaced_labels(management_sel),
                        ],
                        "toPorts": _to_ports([postgres_port]),
                    }
                ],
            },
        }
    )

    policies.append(
        {
            "apiVersion": f"{CILIUM_GROUP}/{CILIUM_VERSION}",
            "kind": "CiliumNetworkPolicy",
            "metadata": {"name": "waddleai-allow-valkey-ingress", "namespace": namespace},
            "spec": {
                "endpointSelector": _namespaced_labels(valkey_sel),
                "ingress": [
                    {
                        "fromEndpoints": [
                            _namespaced_labels(aiproxy_sel),
                            _namespaced_labels(management_sel),
                        ],
                        "toPorts": _to_ports([valkey_port]),
                    }
                ],
            },
        }
    )

    policies.append(
        {
            "apiVersion": f"{CILIUM_GROUP}/{CILIUM_VERSION}",
            "kind": "CiliumNetworkPolicy",
            "metadata": {"name": "waddleai-allow-management-egress", "namespace": namespace},
            "spec": {
                "endpointSelector": _namespaced_labels(management_sel),
                "egress": [
                    {
                        "toEndpoints": [_namespaced_labels(postgres_sel)],
                        "toPorts": _to_ports([postgres_port]),
                    },
                    {
                        "toEndpoints": [_namespaced_labels(valkey_sel)],
                        "toPorts": _to_ports([valkey_port]),
                    },
                    {"toEntities": ["kube-apiserver"]},
                ],
            },
        }
    )

    return policies


# ---------------------------------------------------------------------------
# Reconciler orchestration
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ReconcileStatus:
    """Outcome of one `CiliumPolicyReconciler.reconcile()` run."""

    applied: list[str] = field(default_factory=list)
    skipped: bool = False
    reason: str = ""
    degraded: bool = False


_last_status: ReconcileStatus | None = None


def get_last_status() -> ReconcileStatus | None:
    """Return the most recent reconcile outcome (process-local, best-effort)."""
    return _last_status


class CiliumPolicyReconciler:
    """Control-plane orchestrator: loads DB state, renders CRDs, upserts them.

    Never raises. Every failure path (feature flag off, CRDs absent, k8s API
    error) degrades to a `ReconcileStatus` instead of propagating, so this is
    always safe to call from Management's startup or request path (it never
    enters the AIProxy data path itself — §3.3).
    """

    def __init__(self, db: Any, topology: dict[str, Any] | None = None) -> None:
        """Bind the reconciler to a penguin-dal `db` and an optional topology override."""
        self.db = db
        self._topology_override = topology

    def _topology(self) -> dict[str, Any]:
        if self._topology_override is not None:
            return self._topology_override
        raw = os.environ.get("CILIUM_TOPOLOGY")
        if raw:
            try:
                return dict(json.loads(raw))
            except (TypeError, ValueError) as exc:
                logger.warning("Invalid CILIUM_TOPOLOGY env, using defaults: %s", exc)
        return DEFAULT_TOPOLOGY

    def _load_orgs(self) -> list[tuple[int, str, int | None, bool]]:
        db = self.db
        has_rpm = hasattr(db.organizations, "rpm_limit")
        rows = db(db.organizations.id > 0).select()
        orgs: list[tuple[int, str, int | None, bool]] = []
        for row in rows:
            rpm_limit = getattr(row, "rpm_limit", None) if has_rpm else None
            orgs.append((row.id, row.name, rpm_limit, bool(row.enabled)))
        return orgs

    def reconcile(self) -> ReconcileStatus:
        """Render + upsert this reconciler's full CRD set, never raising.

        Returns a `ReconcileStatus` reflecting whichever of flag-off,
        CRDs-absent, client-unavailable, partial-capability, or a fully (or
        partially, on mid-upsert error) applied run actually happened.
        """
        global _last_status

        if not is_native_rate_limit_enabled():
            status = ReconcileStatus(skipped=True, reason="flag_off")
            _last_status = status
            return status

        caps = cilium_capabilities()
        if not caps["available"]:
            status = ReconcileStatus(skipped=True, reason="crds_absent")
            _last_status = status
            return status

        try:
            client = get_k8s_custom_objects_client()
        except Exception as exc:
            logger.warning("Cannot build Cilium custom objects client, degrading: %s", exc)
            status = ReconcileStatus(skipped=True, reason="client_unavailable", degraded=True)
            _last_status = status
            return status

        topology = self._topology()
        status = ReconcileStatus()

        if caps["envoy_config"]:
            try:
                orgs = self._load_orgs()
            except Exception as exc:
                logger.error("Failed to load organizations for CEC render: %s", exc)
                orgs = []
                status.degraded = True
            cec = render_envoy_config(orgs, topology)
            self._upsert(client, CEC_PLURAL, cec, status)

        if caps["network_policy"]:
            for policy in render_network_policies(topology):
                plural = CNP_PLURAL if policy["kind"] == "CiliumNetworkPolicy" else CCNP_PLURAL
                self._upsert(client, plural, policy, status)

        _last_status = status
        return status

    def _upsert(
        self,
        client: Any,
        plural: str,
        obj: dict[str, Any],
        status: ReconcileStatus,
    ) -> None:
        """Read-then-create-or-replace a single CRD object.

        Wrapped so a failure on this object sets `status.degraded` and
        returns, WITHOUT raising — the caller's loop continues to the next
        object regardless.
        """
        from kubernetes.client.rest import ApiException  # type: ignore[import]

        name = obj["metadata"]["name"]
        namespace = obj["metadata"].get("namespace")

        existing = None
        try:
            if namespace:
                existing = client.get_namespaced_custom_object(
                    CILIUM_GROUP, CILIUM_VERSION, namespace, plural, name
                )
            else:
                existing = client.get_cluster_custom_object(
                    CILIUM_GROUP, CILIUM_VERSION, plural, name
                )
        except ApiException as exc:
            if exc.status != 404:
                logger.warning("Failed to read %s/%s before upsert: %s", plural, name, exc)
                status.degraded = True
                return
        except Exception as exc:
            logger.warning("Failed to read %s/%s before upsert: %s", plural, name, exc)
            status.degraded = True
            return

        try:
            if existing is not None:
                obj["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
                if namespace:
                    client.replace_namespaced_custom_object(
                        CILIUM_GROUP, CILIUM_VERSION, namespace, plural, name, obj
                    )
                else:
                    client.replace_cluster_custom_object(
                        CILIUM_GROUP, CILIUM_VERSION, plural, name, obj
                    )
            elif namespace:
                client.create_namespaced_custom_object(
                    CILIUM_GROUP, CILIUM_VERSION, namespace, plural, obj
                )
            else:
                client.create_cluster_custom_object(CILIUM_GROUP, CILIUM_VERSION, plural, obj)
            status.applied.append(name)
        except Exception as exc:
            logger.error("Failed to upsert %s/%s: %s", plural, name, exc)
            status.degraded = True
