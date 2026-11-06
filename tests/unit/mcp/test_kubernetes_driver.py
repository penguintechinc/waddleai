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


import httpx  # noqa: E402
import pytest  # noqa: E402

from shared.mcp.kubernetes_driver import ORG_LABEL, SERVER_LABEL  # noqa: E402
from shared.mcp.managed import ManagedMcpConflictError  # noqa: E402

_IMG = "ghcr.io/penguintechinc/mcp-codesearch@sha256:" + "a" * 64


def _org_spec(org_id, name="foo"):
    """Build a spec for the given org."""
    return ManagedMcpSpec(name=name, namespace="waddleai-mcp", image=_IMG, org_id=org_id)


def _fake_driver(handler):
    """Return a driver whose HTTP client is backed by a mock transport."""
    driver = KubernetesManagedMcpDriver()
    driver._client = lambda: httpx.AsyncClient(  # type: ignore[method-assign]
        base_url="https://k8s.test", transport=httpx.MockTransport(handler)
    )
    return driver


def test_two_orgs_same_server_name_get_distinct_k8s_names():
    """Verify: org-scoped names and labels differ for same-named servers."""
    a, b = _org_spec(1), _org_spec(2)
    assert a.object_name != b.object_name
    assert a.server_key != b.server_key
    driver = KubernetesManagedMcpDriver()
    for spec in (a, b):
        dep = driver._deployment(spec)
        assert dep["metadata"]["name"] == spec.object_name
        assert dep["metadata"]["labels"][ORG_LABEL] == str(spec.org_id)
        assert dep["metadata"]["labels"][SERVER_LABEL] == spec.server_key
        assert driver._network_policy(spec)["metadata"]["labels"][ORG_LABEL] == str(spec.org_id)
    long = _org_spec(1, "a" * 60)
    assert len(long.object_name) + len("-restricted") <= 63


@pytest.mark.asyncio
async def test_create_after_409_leaves_existing_object_untouched():
    """Verify: a 409 raises a conflict and issues no DELETE."""
    methods = []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(409, json={})

    with pytest.raises(ManagedMcpConflictError):
        await _fake_driver(handler).create(_org_spec(1))
    assert methods == ["POST"]


@pytest.mark.asyncio
async def test_create_failure_deletes_only_objects_this_call_created():
    """Verify: rollback removes the deployment this call made, not the 409 service."""
    deleted = []

    def handler(request):
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(200, json={})
        return httpx.Response(409 if "/services" in request.url.path else 201, json={})

    spec = _org_spec(1)
    with pytest.raises(ManagedMcpConflictError):
        await _fake_driver(handler).create(spec)
    assert deleted == [f"/apis/apps/v1/namespaces/waddleai-mcp/deployments/{spec.object_name}"]


@pytest.mark.asyncio
async def test_delete_refuses_mismatched_labels_and_deletes_matching():
    """Verify: delete is a no-op unless org/server labels match."""
    spec = _org_spec(1)
    for labels, expect_delete in (
        ({ORG_LABEL: "2", SERVER_LABEL: spec.server_key}, False),
        ({ORG_LABEL: "1", SERVER_LABEL: "other"}, False),
        ({}, False),
        ({ORG_LABEL: "1", SERVER_LABEL: spec.server_key}, True),
    ):
        deleted = []

        def handler(request, labels=labels, deleted=deleted):
            if request.method == "DELETE":
                deleted.append(request.url.path)
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"metadata": {"labels": labels}})

        await _fake_driver(handler).delete(spec)
        assert bool(deleted) is expect_delete
        assert len(deleted) == (3 if expect_delete else 0)


def test_k8s_name_unique_constraint_rejects_duplicate():
    """Verify: the DB rejects a second server with the same (namespace, k8s_name)."""
    from sqlalchemy import create_engine
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session

    from services.management.app.models_sqlalchemy import Base, ManagedMcpServer

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[ManagedMcpServer.__table__])

    def row(org_id, name):
        return ManagedMcpServer(
            org_id=org_id,
            name=name,
            namespace="ns",
            k8s_name="mcp-1-foo",
            image=_IMG,
            transport="streamable_http",
        )

    with Session(engine) as session:
        session.add(row(1, "foo"))
        session.commit()
        session.add(row(2, "bar"))
        with pytest.raises(IntegrityError):
            session.commit()
