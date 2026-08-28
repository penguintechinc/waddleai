"""Unit tests for shared.licensing.domain_bypass.

Domain-based license bypass is host-driven only (critical-rules.md Feature
Flags & License Tiers) -- these tests pin the env-var read and the
feature-detecting wiring that stays inert against the currently-pinned
``penguin-licensing`` (no ``set_deployment_host``/``set_extra_bypass_domains``
yet) and activates once that pin is bumped.
"""

from dataclasses import dataclass, field

from shared.licensing.domain_bypass import (
    apply_deployment_host,
    resolve_deployment_host,
)


class TestResolveDeploymentHost:
    """resolve_deployment_host: reads WADDLEAI_PUBLIC_HOST, blank-safe."""

    def test_unset_returns_none(self, monkeypatch):
        """No env var at all -> None, not an empty string."""
        monkeypatch.delenv("WADDLEAI_PUBLIC_HOST", raising=False)
        assert resolve_deployment_host() is None

    def test_blank_returns_none(self, monkeypatch):
        """Whitespace-only value (e.g. an unset Helm default) also -> None."""
        monkeypatch.setenv("WADDLEAI_PUBLIC_HOST", "   ")
        assert resolve_deployment_host() is None

    def test_set_returns_stripped_value(self, monkeypatch):
        """A real host is returned verbatim, surrounding whitespace stripped."""
        monkeypatch.setenv("WADDLEAI_PUBLIC_HOST", " waddleai.penguintech.cloud ")
        assert resolve_deployment_host() == "waddleai.penguintech.cloud"


@dataclass(slots=True)
class _OldPinnedClient:
    """Fakes the currently-pinned penguin-licensing 0.1.0 LicenseClient shape.

    No deployment_host/set_deployment_host/set_extra_bypass_domains --
    apply_deployment_host must be a complete no-op against this shape.
    """

    license_key: str = ""


@dataclass(slots=True)
class _NewPinnedClient:
    """Fakes a future LicenseClient shape carrying penguin-libs#83's setters."""

    deployment_host: str | None = None
    extra_bypass_domains: tuple[str, ...] = field(default_factory=tuple)

    def set_deployment_host(self, host: str | None) -> None:
        """Record the host, mirroring the real setter's contract."""
        self.deployment_host = host

    def set_extra_bypass_domains(self, domains: tuple[str, ...]) -> None:
        """Record the extra domains, mirroring the real setter's contract."""
        self.extra_bypass_domains = tuple(domains)


class TestApplyDeploymentHost:
    """apply_deployment_host: feature-detecting, version-pin-safe wiring."""

    def test_noop_against_client_without_setters(self, monkeypatch):
        """The currently-pinned client shape is untouched -- no AttributeError."""
        monkeypatch.setenv("WADDLEAI_PUBLIC_HOST", "waddleai.penguintech.cloud")
        client = _OldPinnedClient(license_key="PENG-TEST-1234")

        apply_deployment_host(client)  # must not raise

        assert client.license_key == "PENG-TEST-1234"
        assert not hasattr(client, "deployment_host")

    def test_wires_host_from_env_when_supported(self, monkeypatch):
        """Once the client supports it, the resolved env host is applied."""
        monkeypatch.setenv("WADDLEAI_PUBLIC_HOST", "waddleai.penguintech.cloud")
        client = _NewPinnedClient()

        apply_deployment_host(client)

        assert client.deployment_host == "waddleai.penguintech.cloud"

    def test_wires_explicit_host_over_env(self, monkeypatch):
        """An explicitly-passed host wins over WADDLEAI_PUBLIC_HOST."""
        monkeypatch.setenv("WADDLEAI_PUBLIC_HOST", "waddleai.penguintech.cloud")
        client = _NewPinnedClient()

        apply_deployment_host(client, host="waddleai.localhost.local")

        assert client.deployment_host == "waddleai.localhost.local"

    def test_wires_product_domain_as_extra_bypass_domain(self, monkeypatch):
        """WaddleAI's own .app production domain is always wired in by default."""
        monkeypatch.delenv("WADDLEAI_PUBLIC_HOST", raising=False)
        client = _NewPinnedClient()

        apply_deployment_host(client)

        assert client.extra_bypass_domains == ("waddleai.app",)

    def test_no_env_host_leaves_deployment_host_none(self, monkeypatch):
        """No env var and no explicit host -> setter is still called, with None."""
        monkeypatch.delenv("WADDLEAI_PUBLIC_HOST", raising=False)
        client = _NewPinnedClient(deployment_host="stale-value")

        apply_deployment_host(client)

        assert client.deployment_host is None
