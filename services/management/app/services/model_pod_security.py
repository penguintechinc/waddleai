"""Pure builders for the CNP and Tetragon objects that harden self-hosted model pods.

Mirrors k8s/helm/waddleai/templates/model-pod-network-policy.yaml and tetragon-model-policy.yaml
for the pods the fleet manager generates (llama.cpp single + RPC head/workers, Ollama), so a
fleet-created pod gets the same isolation as a chart-rendered one. Settings come from the chart
via ``WADDLEAI_MODEL_POD_SECURITY`` (helper ``waddleai.modelPodSecurity.config``). Every builder
is pure: no Kubernetes client, no I/O.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

ENV_VAR = "WADDLEAI_MODEL_POD_SECURITY"
TRACING_GROUP, TRACING_VERSION = "cilium.io", "v1alpha1"
TRACING_PLURAL = "tracingpoliciesnamespaced"
TRACING_KIND = "TracingPolicyNamespaced"
DEFAULT_PRIVATE_CIDRS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "100.64.0.0/10",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
)
HF_FQDNS = (
    "huggingface.co",
    "cdn-lfs.huggingface.co",
    "cdn-lfs-us-1.hf.co",
    "cdn-lfs-eu-1.hf.co",
    "cas-bridge.xethub.hf.co",
    "cas-server.xethub.hf.co",
    "transfer.xethub.hf.co",
)
OLLAMA_FQDNS = (
    "registry.ollama.ai",
    "ollama.com",
    "dd20bb891979d25aebc8bec07b2b3bbc.r2.cloudflarestorage.com",
)
OWNER_LABEL = "waddleai.penguintech.io/model-pod-owner"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_FLEET = "waddleai-fleet"
# management -> engine, least privilege (L7): only what the manager actually calls
OLLAMA_MGMT_HTTP = (
    {"method": "DELETE", "path": "^/api/delete$"},
    {"method": "GET", "path": "^/$"},
    {"method": "GET", "path": "^/api/tags$"},
    {"method": "POST", "path": "^/api/pull$"},
)
LLAMA_MGMT_HTTP = ({"method": "GET", "path": "^/health$"},)
DEFAULT_CLUSTER_DOMAIN = "cluster.local"
DEFAULT_PROXY_SELECTOR = {
    "app.kubernetes.io/name": "waddleai",
    "app.kubernetes.io/component": "proxy",
}
DEFAULT_MANAGEMENT_SELECTOR = {
    "app.kubernetes.io/name": "waddleai",
    "app.kubernetes.io/component": "management",
}
LLAMA_SERVE_EXECS = ("/app/llama-server", "/llama-server", "/usr/local/bin/llama-server")
RPC_SERVE_EXECS = ("/usr/local/bin/rpc-server",)
LLAMA_PULL_EXECS = ("/bin/sh", "/usr/bin/curl", "/bin/busybox")
RPC_WAIT_EXECS = (*LLAMA_PULL_EXECS, "/bin/sleep", "/usr/bin/sleep")
OLLAMA_EXECS = ("/usr/bin/ollama",)


@dataclass(frozen=True)
class Preset:
    """One model-pull egress preset (HuggingFace or Ollama registry)."""

    enabled: bool = True
    fqdns: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelPodSecurityConfig:
    """Settings for the generated model-pod policies (defaults match values.yaml)."""

    network_policy: bool = False
    tetragon: bool = False
    tetragon_mode: str = "enforce"
    management_ingress: bool = True
    private_cidrs: tuple[str, ...] = DEFAULT_PRIVATE_CIDRS
    cluster_domain: str = DEFAULT_CLUSTER_DOMAIN
    dns_cluster_patterns: tuple[str, ...] = ()  # empty = derive ``*.<cluster_domain>``
    huggingface: Preset = field(default_factory=lambda: Preset(True, HF_FQDNS, ("*.hf.co",)))
    ollama: Preset = field(default_factory=lambda: Preset(True, OLLAMA_FQDNS, ()))
    extra_fqdns: tuple[str, ...] = ()
    proxy_selector: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PROXY_SELECTOR))
    management_selector: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_MANAGEMENT_SELECTOR)
    )
    llamacpp_serve_execs: tuple[str, ...] = LLAMA_SERVE_EXECS
    llamacpp_pull_execs: tuple[str, ...] = LLAMA_PULL_EXECS
    ollama_execs: tuple[str, ...] = OLLAMA_EXECS

    @property
    def action(self) -> str:
        """Tetragon action: Sigkill when enforcing, Post when observing."""
        return "Sigkill" if self.tetragon_mode == "enforce" else "Post"


def _preset(raw: Any, default: Preset) -> Preset:
    """Parse one preset mapping, keeping the defaults for anything absent."""
    if not isinstance(raw, dict):
        return default
    return Preset(
        enabled=bool(raw.get("enabled", default.enabled)),
        fqdns=tuple(raw.get("fqdns", default.fqdns)),
        patterns=tuple(raw.get("patterns", default.patterns)),
    )


def load_config(raw: str | None = None) -> ModelPodSecurityConfig:
    """Parse the chart-provided JSON; unset means the chart did not provision (policies off).

    Malformed JSON raises: a policy set silently dropped because of a typo is worse than a loud
    failure at deploy time.
    """
    text = os.environ.get(ENV_VAR, "") if raw is None else raw
    if not text.strip():
        logger.info("%s unset: model-pod policies are not generated", ENV_VAR)
        return ModelPodSecurityConfig()
    data = json.loads(text)
    base = ModelPodSecurityConfig()
    mode = str(data.get("tetragon_mode", "enforce"))
    if mode not in ("enforce", "observe"):
        raise ValueError(f"tetragon_mode must be enforce|observe, got {mode!r}")
    return ModelPodSecurityConfig(
        network_policy=bool(data.get("network_policy", False)),
        tetragon=bool(data.get("tetragon", False)),
        tetragon_mode=mode,
        management_ingress=bool(data.get("management_ingress", True)),
        private_cidrs=tuple(data.get("private_cidrs", base.private_cidrs)),
        cluster_domain=str(data.get("cluster_domain") or base.cluster_domain),
        dns_cluster_patterns=tuple(data.get("dns_cluster_patterns") or ()),
        huggingface=_preset(data.get("huggingface"), base.huggingface),
        ollama=_preset(data.get("ollama"), base.ollama),
        extra_fqdns=tuple(data.get("extra_fqdns", ())),
        proxy_selector=dict(data.get("proxy_selector") or base.proxy_selector),
        management_selector=dict(data.get("management_selector") or base.management_selector),
        llamacpp_serve_execs=tuple(data.get("llamacpp_serve_execs", base.llamacpp_serve_execs)),
        llamacpp_pull_execs=tuple(data.get("llamacpp_pull_execs", base.llamacpp_pull_execs)),
        ollama_execs=tuple(data.get("ollama_execs", base.ollama_execs)),
    )


def pull_hosts(
    cfg: ModelPodSecurityConfig, *, hf: bool, ollama: bool
) -> tuple[list[str], list[str]]:
    """``(names, patterns)`` the enabled presets and extraFQDNs allow, de-duplicated."""
    names: list[str] = []
    patterns: list[str] = []
    for wanted, preset in ((hf, cfg.huggingface), (ollama, cfg.ollama)):
        if wanted and preset.enabled:
            names += preset.fqdns
            patterns += preset.patterns
    names += cfg.extra_fqdns
    return list(dict.fromkeys(names)), list(dict.fromkeys(patterns))


def dns_rule(
    cfg: ModelPodSecurityConfig,
    *,
    hf: bool = False,
    ollama: bool = False,
    extra_patterns: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """kube-dns egress whose DNS proxy rule resolves only the names this pod needs.

    toFQDNs only matches names Cilium saw resolved, so the proxy rule is required; it is limited
    to the pull hosts, the cluster-service patterns (search-domain expansions and peer names) and
    ``extra_patterns`` instead of ``*``.
    """
    names, patterns = pull_hosts(cfg, hf=hf, ollama=ollama)
    rules: list[dict[str, str]] = [{"matchName": n} for n in names]
    cluster = cfg.dns_cluster_patterns or (f"*.{cfg.cluster_domain}",)
    allowed = [*patterns, *cluster, *extra_patterns]
    rules += [{"matchPattern": p} for p in dict.fromkeys(allowed)]
    return {
        "toEndpoints": [
            {
                "matchLabels": {
                    "k8s:io.kubernetes.pod.namespace": "kube-system",
                    "k8s-app": "kube-dns",
                }
            }
        ],
        "toPorts": [{"ports": [{"port": "53", "protocol": "ANY"}], "rules": {"dns": rules}}],
    }


def pull_rule(cfg: ModelPodSecurityConfig, *, hf: bool, ollama: bool) -> dict[str, Any] | None:
    """TCP/443-only toFQDNs rule for the enabled presets plus extra FQDNs; None when all are off."""
    names, patterns = pull_hosts(cfg, hf=hf, ollama=ollama)
    fqdns = [{"matchName": n} for n in names] + [{"matchPattern": p} for p in patterns]
    if not fqdns:
        return None
    return {"toFQDNs": fqdns, "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}]}


def owner_of(labels: dict[str, str]) -> str | None:
    """The deployment public id carried by a selector (the label whose key ends ``-public-id``)."""
    return next((v for k, v in labels.items() if k.endswith("-public-id")), None)


def _meta(name: str, namespace: str, labels: dict[str, str]) -> dict[str, Any]:
    """Object metadata: fleet-owned marker + owner public id (used to adopt on a 409)."""
    meta: dict[str, Any] = {
        "name": name,
        "namespace": namespace,
        "labels": {MANAGED_BY_LABEL: MANAGED_BY_FLEET},
    }
    owner = owner_of(labels)
    if owner:
        meta["labels"][OWNER_LABEL] = owner
    return meta


def cnp(
    *,
    name: str,
    namespace: str,
    labels: dict[str, str],
    description: str,
    cfg: ModelPodSecurityConfig,
    ports: list[int],
    hf: bool = False,
    ollama: bool = False,
    peer_ingress: list[dict[str, Any]] | None = None,
    peer_egress: list[dict[str, Any]] | None = None,
    serving: bool = True,
    auth: dict[str, str] | None = None,
    mgmt_http: tuple[dict[str, str], ...] | None = None,
    dns_extra: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """CNP for one model pod set: proxy (and management) in on ``ports``, DNS + peers + pull out.

    ``labels`` must carry the per-deployment public-id label so the policy (and every peer
    selector derived from it) cannot match another deployment's or org's pods. ``serving=False``
    (workers) omits the proxy/management ingress entirely.
    """
    ingress: list[dict[str, Any]] = []
    if serving:
        port_list = [{"port": str(p), "protocol": "TCP"} for p in ports]
        proxy_rule: dict[str, Any] = {
            "fromEndpoints": [{"matchLabels": dict(cfg.proxy_selector)}],
            "toPorts": [{"ports": port_list}],
        }
        if auth:
            proxy_rule["authentication"] = auth
        ingress.append(proxy_rule)
        if cfg.management_ingress:
            # management may only make the calls the manager code makes (L7), never full access
            mgmt_ports: dict[str, Any] = {"ports": port_list}
            if mgmt_http:
                mgmt_ports["rules"] = {"http": [dict(r) for r in mgmt_http]}
            mgmt_rule: dict[str, Any] = {
                "fromEndpoints": [{"matchLabels": dict(cfg.management_selector)}],
                "toPorts": [mgmt_ports],
            }
            if auth:
                mgmt_rule["authentication"] = auth
            ingress.append(mgmt_rule)
    ingress += peer_ingress or []
    egress: list[dict[str, Any]] = [
        dns_rule(cfg, hf=hf, ollama=ollama, extra_patterns=dns_extra),
        *(peer_egress or []),
    ]
    pull = pull_rule(cfg, hf=hf, ollama=ollama)
    if pull:
        egress.append(pull)
    return {
        "apiVersion": "cilium.io/v2",
        "kind": "CiliumNetworkPolicy",
        "metadata": _meta(name, namespace, labels),
        "spec": {
            "description": description,
            "endpointSelector": {"matchLabels": dict(labels)},
            "ingress": ingress,
            "egress": egress,
        },
    }


def _tracing(
    name: str,
    namespace: str,
    labels: dict[str, str],
    containers: list[str] | None,
    kprobe: dict[str, Any],
) -> dict[str, Any]:
    """One TracingPolicyNamespaced scoped by pod labels and (optionally) container names."""
    spec: dict[str, Any] = {"podSelector": {"matchLabels": dict(labels)}, "kprobes": [kprobe]}
    if containers:
        spec["containerSelector"] = {
            "matchExpressions": [{"key": "name", "operator": "In", "values": list(containers)}]
        }
    return {
        "apiVersion": f"{TRACING_GROUP}/{TRACING_VERSION}",
        "kind": TRACING_KIND,
        "metadata": _meta(name, namespace, labels),
        "spec": spec,
    }


def exec_policy(
    *,
    name: str,
    namespace: str,
    labels: dict[str, str],
    allowed: tuple[str, ...] | list[str],
    cfg: ModelPodSecurityConfig,
    containers: list[str] | None = None,
    allowed_prefixes: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Allowlist of executables: any other binary is killed (shells/package managers excluded).

    Hooks ``security_bprm_check`` (the resolved binary being executed, for execve AND execveat,
    and again for a ``#!`` interpreter) instead of matching ``execve``'s arg0 string, which
    ``execveat``/fexecve and symlink names bypass. Paths are therefore REAL paths (symlinks
    resolved). ``allowed_prefixes`` covers toolchain trees with versioned paths (gcc's cc1).
    """
    if not allowed:
        raise ValueError("an exec allowlist must not be empty (that would kill every exec)")
    match_args: list[dict[str, Any]] = [
        {"index": 0, "operator": "NotEqual", "values": list(allowed)}
    ]
    if allowed_prefixes:
        match_args.append({"index": 0, "operator": "NotPrefix", "values": list(allowed_prefixes)})
    kprobe = {
        "call": "security_bprm_check",
        "syscall": False,
        "args": [{"index": 0, "type": "linux_binprm"}],
        "selectors": [{"matchArgs": match_args, "matchActions": [{"action": cfg.action}]}],
    }
    return _tracing(name, namespace, labels, containers, kprobe)


def readonly_policy(
    *,
    name: str,
    namespace: str,
    labels: dict[str, str],
    paths: list[str],
    cfg: ModelPodSecurityConfig,
    containers: list[str] | None = None,
) -> dict[str, Any]:
    """Kill any write/append open-for-write under the weights paths."""
    kprobe = {
        "call": "security_file_permission",
        "syscall": False,
        "args": [{"index": 0, "type": "file"}, {"index": 1, "type": "int"}],
        "selectors": [
            {
                "matchArgs": [
                    {"index": 0, "operator": "Prefix", "values": list(paths)},
                    {"index": 1, "operator": "Equal", "values": ["2", "8"]},
                ],
                "matchActions": [{"action": cfg.action}],
            }
        ],
    }
    return _tracing(name, namespace, labels, containers, kprobe)


def egress_policy(
    *,
    name: str,
    namespace: str,
    labels: dict[str, str],
    cfg: ModelPodSecurityConfig,
    containers: list[str] | None = None,
    allow_public: bool = False,
) -> dict[str, Any]:
    """connect() outside the private CIDRs is killed; ``allow_public`` (pull containers) only logs.

    A kprobe cannot match FQDNs, so for pull-capable containers the CNP FQDN allowlist is the
    control and this policy just records the connect.
    """
    action = "Post" if allow_public else cfg.action
    kprobe = {
        "call": "tcp_connect",
        "syscall": False,
        "args": [{"index": 0, "type": "sock"}],
        "selectors": [
            {
                "matchArgs": [
                    {"index": 0, "operator": "NotDAddr", "values": list(cfg.private_cidrs)}
                ],
                "matchActions": [{"action": action}],
            }
        ],
    }
    return _tracing(name, namespace, labels, containers, kprobe)


def serve_policies(
    *,
    base: str,
    namespace: str,
    labels: dict[str, str],
    cfg: ModelPodSecurityConfig,
    execs: tuple[str, ...] | list[str],
    serve_containers: list[str] | None,
    pull_containers: list[str] | None = None,
    pull_execs: tuple[str, ...] | list[str] = (),
    readonly_paths: list[str] | None = None,
    pods_pull_wide: bool = False,
) -> list[dict[str, Any]]:
    """The exec / pull-exec / read-only / egress Tetragon policies for one pod set."""
    out = [
        exec_policy(
            name=f"{base}-exec",
            namespace=namespace,
            labels=labels,
            allowed=execs,
            cfg=cfg,
            containers=serve_containers,
        )
    ]
    if pull_containers and pull_execs:
        out.append(
            exec_policy(
                name=f"{base}-pull-exec",
                namespace=namespace,
                labels=labels,
                allowed=pull_execs,
                cfg=cfg,
                containers=pull_containers,
            )
        )
    if readonly_paths:
        out.append(
            readonly_policy(
                name=f"{base}-readonly",
                namespace=namespace,
                labels=labels,
                paths=readonly_paths,
                cfg=cfg,
                containers=serve_containers,
            )
        )
    out.append(
        egress_policy(
            name=f"{base}-egress",
            namespace=namespace,
            labels=labels,
            cfg=cfg,
            containers=serve_containers,
            allow_public=pods_pull_wide,
        )
    )
    if pull_containers:
        out.append(
            egress_policy(
                name=f"{base}-pull-egress",
                namespace=namespace,
                labels=labels,
                cfg=cfg,
                containers=pull_containers,
                allow_public=True,
            )
        )
    return out


def apply_policy(custom: Any, namespace: str, doc: dict[str, Any]) -> str:
    """Create ``doc``; on a 409 adopt it (replace) only if it belongs to the same deployment.

    Returns ``created`` or ``replaced``. A 409 on an object owned by another deployment (or not
    fleet-managed at all) raises: never overwrite someone else's policy.
    """
    group, version, plural = (
        ("cilium.io", "v2", "ciliumnetworkpolicies")
        if doc["kind"] == "CiliumNetworkPolicy"
        else (TRACING_GROUP, TRACING_VERSION, TRACING_PLURAL)
    )
    try:
        custom.create_namespaced_custom_object(group, version, namespace, plural, doc)
        return "created"
    except Exception as exc:
        if getattr(exc, "status", None) != 409:
            raise
    name = doc["metadata"]["name"]
    existing = custom.get_namespaced_custom_object(group, version, namespace, plural, name)
    have = (existing.get("metadata") or {}).get("labels") or {}
    want = doc["metadata"]["labels"]
    if have.get(OWNER_LABEL) is None or have.get(OWNER_LABEL) != want.get(OWNER_LABEL):
        raise RuntimeError(
            f"{doc['kind']}/{name} exists and belongs to another owner; not adopting"
        )
    replacement = {**doc, "metadata": {**doc["metadata"]}}
    version_ = (existing.get("metadata") or {}).get("resourceVersion")
    if version_:
        replacement["metadata"]["resourceVersion"] = version_
    custom.replace_namespaced_custom_object(group, version, namespace, plural, name, replacement)
    return "replaced"


def delete_policy(custom: Any, namespace: str, kind: str, name: str) -> bool:
    """Delete one policy; True when it is gone (a 404 counts), False on any other failure."""
    group, version, plural = (
        ("cilium.io", "v2", "ciliumnetworkpolicies")
        if kind == "CiliumNetworkPolicy"
        else (TRACING_GROUP, TRACING_VERSION, TRACING_PLURAL)
    )
    try:
        custom.delete_namespaced_custom_object(group, version, namespace, plural, name)
    except Exception as exc:
        if getattr(exc, "status", None) == 404:
            return True
        logger.error("failed to delete %s %s: %s", kind, name, exc)
        return False
    return True
