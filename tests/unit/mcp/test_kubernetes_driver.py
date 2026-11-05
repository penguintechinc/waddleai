"""Security invariants of the managed-MCP Kubernetes manifest generator."""

from shared.mcp.kubernetes_driver import KubernetesManagedMcpDriver
from shared.mcp.managed import ManagedMcpSpec


def _spec():
    return ManagedMcpSpec(
        name="code-search",
        namespace="waddleai-mcp",
        image="ghcr.io/penguintechinc/mcp-codesearch@sha256:" + "a" * 64,
    )


def test_generated_workload_is_non_privileged_and_network_restricted():
    """Verify: generated workload is non privileged and network restricted."""
    driver = KubernetesManagedMcpDriver()
    container = driver._deployment(_spec())["spec"]["template"]["spec"]["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    policy = driver._network_policy(_spec())["spec"]
    assert policy["policyTypes"] == ["Ingress", "Egress"]
    assert policy["egress"][0]["ports"][0]["port"] == 53
