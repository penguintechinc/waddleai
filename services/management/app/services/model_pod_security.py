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
        huggingface=_preset(data.get("huggingface"), base.huggingface),
        ollama=_preset(data.get("ollama"), base.ollama),
        extra_fqdns=tuple(data.get("extra_fqdns", ())),
        proxy_selector=dict(data.get("proxy_selector") or base.proxy_selector),
        management_selector=dict(data.get("management_selector") or base.management_selector),
        llamacpp_serve_execs=tuple(data.get("llamacpp_serve_execs", base.llamacpp_serve_execs)),
        llamacpp_pull_execs=tuple(data.get("llamacpp_pull_execs", base.llamacpp_pull_execs)),
        ollama_execs=tuple(data.get("ollama_execs", base.ollama_execs)),
    )


def dns_rule() -> dict[str, Any]:
    """kube-dns egress WITH the DNS proxy rule (toFQDNs only matches names Cilium saw resolved)."""
    return {
        "toEndpoints": [
            {
                "matchLabels": {
                    "k8s:io.kubernetes.pod.namespace": "kube-system",
                    "k8s-app": "kube-dns",
                }
            }
        ],
        "toPorts": [
            {
                "ports": [{"port": "53", "protocol": "ANY"}],
                "rules": {"dns": [{"matchPattern": "*"}]},
            }
        ],
    }


def pull_rule(cfg: ModelPodSecurityConfig, *, hf: bool, ollama: bool) -> dict[str, Any] | None:
    """TCP/443-only toFQDNs rule for the enabled presets plus extra FQDNs; None when all are off."""
    names: list[str] = []
    patterns: list[str] = []
    for wanted, preset in ((hf, cfg.huggingface), (ollama, cfg.ollama)):
        if wanted and preset.enabled:
            names += preset.fqdns
            patterns += preset.patterns
    names += cfg.extra_fqdns
    fqdns = [{"matchName": n} for n in dict.fromkeys(names)]
    fqdns += [{"matchPattern": p} for p in dict.fromkeys(patterns)]
    if not fqdns:
        return None
    return {"toFQDNs": fqdns, "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}]}


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
) -> dict[str, Any]:
    """CNP for one model pod set: proxy (and management) in on ``ports``, DNS + peers + pull out.

    ``labels`` must carry the per-deployment public-id label so the policy (and every peer
    selector derived from it) cannot match another deployment's or org's pods. ``serving=False``
    (workers) omits the proxy/management ingress entirely.
    """
    ingress: list[dict[str, Any]] = []
    if serving:
        sources = [{"matchLabels": dict(cfg.proxy_selector)}]
        if cfg.management_ingress:
            sources.append({"matchLabels": dict(cfg.management_selector)})
        rule: dict[str, Any] = {
            "fromEndpoints": sources,
            "toPorts": [{"ports": [{"port": str(p), "protocol": "TCP"} for p in ports]}],
        }
        if auth:
            rule["authentication"] = auth
        ingress.append(rule)
    ingress += peer_ingress or []
    egress: list[dict[str, Any]] = [dns_rule(), *(peer_egress or [])]
    pull = pull_rule(cfg, hf=hf, ollama=ollama)
    if pull:
        egress.append(pull)
    return {
        "apiVersion": "cilium.io/v2",
        "kind": "CiliumNetworkPolicy",
        "metadata": {"name": name, "namespace": namespace},
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
        "metadata": {"name": name, "namespace": namespace},
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
) -> dict[str, Any]:
    """Allowlist of executables: any other execve is killed (shells/package managers excluded)."""
    if not allowed:
        raise ValueError("an exec allowlist must not be empty (that would kill every exec)")
    kprobe = {
        "call": "sys_execve",
        "syscall": True,
        "args": [{"index": 0, "type": "string"}],
        "selectors": [
            {
                "matchArgs": [{"index": 0, "operator": "NotIn", "values": list(allowed)}],
                "matchActions": [{"action": cfg.action}],
            }
        ],
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
