"""Render assertions for the hardened-Ollama Helm wiring (plan Task 14).

Covers: DaemonSet/Deployment + initContainers consume the hardened image;
every fleet container is non-root/no-priv-esc/readOnlyRootFS/seccomp-hardened
with capabilities dropped; only the model-store + tmp mounts are writable;
`ollama.mode=pool` renders a Deployment instead of a DaemonSet (and vice
versa), never both.
"""

from tests.helm.conftest import find, render

HARDENED_IMAGE = "ghcr.io/penguintechinc/waddleai/ollama:hardened"

RESTRICTED_SET_VALUES = {
    "ollama.enabled": "true",
    "ollama.models[0]": "llama3:8b",
}


def _assert_restricted(container: dict) -> None:
    """A single container satisfies PSA `restricted` plus seccomp RuntimeDefault."""
    sc = container["securityContext"]
    assert sc["runAsNonRoot"] is True
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"]["drop"] == ["ALL"]
    assert sc["readOnlyRootFilesystem"] is True
    assert sc["seccompProfile"]["type"] == "RuntimeDefault"


def _assert_only_model_and_tmp_writable(container: dict) -> None:
    mount_names = {m["name"] for m in container.get("volumeMounts", [])}
    assert mount_names <= {"ollama-models", "tmp"}, mount_names


class TestDaemonSetMode:
    """Default mode (ollama.mode unset / "daemonset")."""

    def test_daemonset_renders_not_deployment(self):
        docs = render("values-alpha.yaml", RESTRICTED_SET_VALUES)
        assert any(d["kind"] == "DaemonSet" and d["metadata"]["name"] == "waddleai-ollama" for d in docs)
        assert not any(
            d["kind"] == "Deployment" and d["metadata"]["name"] == "waddleai-ollama" for d in docs
        )

    def test_serve_sidecar_and_pull_initcontainers_use_hardened_image(self):
        docs = render("values-alpha.yaml", RESTRICTED_SET_VALUES)
        ds = find(docs, "DaemonSet", "waddleai-ollama")
        init = {c["name"]: c for c in ds["spec"]["template"]["spec"]["initContainers"]}
        assert set(init) == {"serve", "pull-llama3-8b"}
        for c in init.values():
            assert c["image"] == HARDENED_IMAGE

    def test_serve_sidecar_is_native_sidecar_with_readiness_gate(self):
        docs = render("values-alpha.yaml", RESTRICTED_SET_VALUES)
        ds = find(docs, "DaemonSet", "waddleai-ollama")
        serve = next(
            c for c in ds["spec"]["template"]["spec"]["initContainers"] if c["name"] == "serve"
        )
        assert serve["restartPolicy"] == "Always"
        assert serve["readinessProbe"]["exec"]["command"] == ["/usr/bin/ollama", "list"]

    def test_pull_initcontainer_targets_loopback_not_shell(self):
        docs = render("values-alpha.yaml", RESTRICTED_SET_VALUES)
        ds = find(docs, "DaemonSet", "waddleai-ollama")
        pull = next(
            c
            for c in ds["spec"]["template"]["spec"]["initContainers"]
            if c["name"] == "pull-llama3-8b"
        )
        assert pull["args"] == ["pull", "llama3:8b"]
        env = {e["name"]: e["value"] for e in pull["env"]}
        assert env["OLLAMA_HOST"] == "127.0.0.1:11434"
        # No `command` override anywhere — the image's ENTRYPOINT is the
        # binary itself, never a shell wrapper.
        assert "command" not in pull

    def test_main_container_uses_hardened_image(self):
        docs = render("values-alpha.yaml", RESTRICTED_SET_VALUES)
        ds = find(docs, "DaemonSet", "waddleai-ollama")
        main = next(
            c for c in ds["spec"]["template"]["spec"]["containers"] if c["name"] == "ollama"
        )
        assert main["image"] == HARDENED_IMAGE

    def test_every_fleet_container_is_restricted_and_scoped(self):
        docs = render("values-alpha.yaml", RESTRICTED_SET_VALUES)
        ds = find(docs, "DaemonSet", "waddleai-ollama")
        podspec = ds["spec"]["template"]["spec"]
        for c in podspec["initContainers"] + podspec["containers"]:
            _assert_restricted(c)
            _assert_only_model_and_tmp_writable(c)

    def test_no_shell_invoked_anywhere_in_pod_spec(self):
        """Regression guard: no container in this pod uses /bin/sh -c."""
        docs = render("values-alpha.yaml", RESTRICTED_SET_VALUES)
        ds = find(docs, "DaemonSet", "waddleai-ollama")
        podspec = ds["spec"]["template"]["spec"]
        for c in podspec["initContainers"] + podspec["containers"]:
            command = c.get("command") or []
            assert "/bin/sh" not in command and "sh" not in command


class TestPoolMode:
    """ollama.mode: pool."""

    POOL_VALUES = {**RESTRICTED_SET_VALUES, "ollama.mode": "pool"}

    def test_deployment_renders_not_daemonset(self):
        docs = render("values-alpha.yaml", self.POOL_VALUES)
        assert any(
            d["kind"] == "Deployment" and d["metadata"]["name"] == "waddleai-ollama" for d in docs
        )
        assert not any(
            d["kind"] == "DaemonSet" and d["metadata"]["name"] == "waddleai-ollama" for d in docs
        )

    def test_replica_count_from_values(self):
        docs = render("values-alpha.yaml", self.POOL_VALUES)
        dep = find(docs, "Deployment", "waddleai-ollama")
        assert dep["spec"]["replicas"] == 2  # values.yaml default ollama.pool.replicaCount

    def test_pod_spec_identical_shape_to_daemonset_mode(self):
        """Same podTemplate helper — pool mode still gets the hardened image + restricted containers."""
        docs = render("values-alpha.yaml", self.POOL_VALUES)
        dep = find(docs, "Deployment", "waddleai-ollama")
        podspec = dep["spec"]["template"]["spec"]
        for c in podspec["initContainers"] + podspec["containers"]:
            _assert_restricted(c)


class TestFeatureOff:
    """ollama.enabled: false (the values.yaml/values-alpha.yaml default) renders nothing."""

    def test_no_ollama_objects_when_disabled(self):
        docs = render("values-alpha.yaml")
        names = {d["metadata"]["name"] for d in docs if d.get("kind") in ("DaemonSet", "Deployment")}
        assert "waddleai-ollama" not in names
