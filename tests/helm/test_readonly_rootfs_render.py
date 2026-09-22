"""Rendered-manifest assertions for read-only root filesystems.

regression: audit-2026-09-14 — trivy KSV-0014 (HIGH) against the management and
webui Deployments: neither set ``readOnlyRootFilesystem``, so both ran with a
writable container root.

These assert against real ``helm template`` output rather than the template
source, because the management container's securityContext is merged from
``.Values.securityContext`` at render time — reading the template text would
not prove what actually reaches the cluster.
"""

import pytest

from tests.helm.conftest import find, render

# Every writable path must be backed by a volume; a readOnlyRootFilesystem with
# a missing mount is a crash-on-start, not a hardening win.
# noqa: S108 below -- these are container mountPath strings asserted against
# rendered Kubernetes manifests, not filesystem paths this process opens.
EXPECTED_MOUNTS = {
    "waddleai-management": {  # noqa: S108
        "/tmp",  # noqa: S108
        "/app/logs",
        "/app/databases",
        "/app/config/marchproxy",
    },
    # /var/log/nginx is deliberately absent: the base image symlinks the nginx
    # logs to /dev/stdout and /dev/stderr, and mounting over it would divert
    # container logs into an emptyDir.
    "waddleai-webui": {"/tmp", "/var/cache/nginx", "/var/run"},  # noqa: S108
}


def _container(docs, deployment_name):
    """Return the single app container of the named Deployment."""
    dep = find(docs, "Deployment", deployment_name)
    containers = dep["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1, f"{deployment_name} has {len(containers)} containers, expected 1"
    return dep, containers[0]


@pytest.mark.parametrize("values_file", ["values.yaml", "values-alpha.yaml", "values-beta.yaml"])
@pytest.mark.parametrize("deployment_name", sorted(EXPECTED_MOUNTS))
class TestReadOnlyRootFilesystem:
    """regression: audit-2026-09-14 — KSV-0014 on management and webui."""

    def test_read_only_root_filesystem_is_true(self, values_file, deployment_name):
        """The container securityContext sets readOnlyRootFilesystem: true."""
        docs = render(values_file)
        _, container = _container(docs, deployment_name)
        sec_ctx = container.get("securityContext") or {}
        assert sec_ctx.get("readOnlyRootFilesystem") is True, (
            f"{deployment_name} in {values_file} renders "
            f"readOnlyRootFilesystem={sec_ctx.get('readOnlyRootFilesystem')!r}"
        )

    def test_writable_paths_are_mounted(self, values_file, deployment_name):
        """Every path the workload writes to is backed by a volume mount."""
        docs = render(values_file)
        _, container = _container(docs, deployment_name)
        mounted = {m["mountPath"] for m in container.get("volumeMounts") or []}
        missing = EXPECTED_MOUNTS[deployment_name] - mounted
        assert not missing, (
            f"{deployment_name} in {values_file} is missing mounts: {sorted(missing)}"
        )

    def test_every_mount_resolves_to_a_volume(self, values_file, deployment_name):
        """No volumeMount references a volume the pod spec never declares."""
        docs = render(values_file)
        dep, container = _container(docs, deployment_name)
        declared = {v["name"] for v in dep["spec"]["template"]["spec"].get("volumes") or []}
        referenced = {m["name"] for m in container.get("volumeMounts") or []}
        assert referenced, f"{deployment_name} declares no volumeMounts"
        assert referenced <= declared, (
            f"{deployment_name} mounts undeclared volumes: {sorted(referenced - declared)}"
        )

    def test_hardening_baseline_still_holds(self, values_file, deployment_name):
        """The rest of the container securityContext is not regressed by the merge."""
        docs = render(values_file)
        _, container = _container(docs, deployment_name)
        sec_ctx = container.get("securityContext") or {}
        assert sec_ctx.get("runAsNonRoot") is True
        assert sec_ctx.get("allowPrivilegeEscalation") is False
        assert (sec_ctx.get("capabilities") or {}).get("drop") == ["ALL"]
        assert (sec_ctx.get("seccompProfile") or {}).get("type") == "RuntimeDefault"
