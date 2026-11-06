"""Wave-5 managed MCP lifecycle safety tests."""

import pytest

from shared.mcp.managed import ManagedMcpLifecycle, ManagedMcpSpec, ManagedMcpValidationError


class Driver:
    """Test double for Driver."""

    def __init__(self, *, fail_discovery=False):
        """Initialise the test double."""
        self.calls = []
        self.fail_discovery = fail_discovery

    async def create(self, spec):
        """Test helper: create."""
        self.calls.append(("create", spec.name))

    async def wait_ready(self, spec):
        """Test helper: wait ready."""
        self.calls.append(("ready", spec.name))
        return "https://codesearch.mcp.svc/mcp"

    async def discover_tools(self, url, transport):
        """Test helper: discover tools."""
        self.calls.append(("discover", url))
        if self.fail_discovery:
            raise RuntimeError("bad tool manifest")
        return ["search", "search", "status"]

    async def delete(self, spec):
        """Test helper: delete."""
        self.calls.append(("delete", spec.name))


def spec(**overrides):
    """Test helper: spec."""
    values = {
        "name": "code-search",
        "image": "ghcr.io/penguintechinc/mcp-codesearch@sha256:" + "a" * 64,
        "namespace": "waddleai-mcp",
    }
    values.update(overrides)
    return ManagedMcpSpec(**values)


@pytest.mark.asyncio
async def test_lifecycle_requires_safe_contract_and_discovers_before_active():
    """Verify: lifecycle requires safe contract and discovers before active."""
    driver = Driver()
    endpoint = await ManagedMcpLifecycle(driver, allowed_namespaces={"waddleai-mcp"}).provision(
        spec()
    )
    assert endpoint.status == "active"
    assert endpoint.discovered_tools == ("search", "status")
    assert [call[0] for call in driver.calls] == ["create", "ready", "discover"]


@pytest.mark.asyncio
async def test_lifecycle_cleans_up_when_discovery_fails():
    """Verify: lifecycle cleans up when discovery fails."""
    driver = Driver(fail_discovery=True)
    with pytest.raises(RuntimeError, match="bad tool manifest"):
        await ManagedMcpLifecycle(driver, allowed_namespaces={"waddleai-mcp"}).provision(spec())
    assert [call[0] for call in driver.calls] == ["create", "ready", "discover", "delete"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"image": "ghcr.io/penguintechinc/mcp-codesearch:latest"},
        {"namespace": "default"},
        {"network_policy": "allow-all"},
        {"service_account": "admin"},
    ],
)
def test_lifecycle_rejects_unsafe_spec(overrides):
    """Verify: lifecycle rejects unsafe spec."""
    with pytest.raises(ManagedMcpValidationError):
        spec(**overrides).validate({"waddleai-mcp"})
