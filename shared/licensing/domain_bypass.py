"""Deployment-host wiring for ``penguin_licensing``'s domain-based bypass.

Managed PenguinTech domains (PenguinCloud, the beta/dev cluster, WaddleAI's
own production domain) skip live license-server round trips entirely --
bypass is domain-driven only, never an env var/CLI flag/config toggle (see
``critical-rules.md`` Feature Flags & License Tiers). ``penguin_licensing``
gained ``LicenseClient.deployment_host``/``set_deployment_host`` support in
penguintechinc/penguin-libs#83, not yet released to PyPI as of this
module's writing -- see ``requirements.in``'s pinned ``penguin-licensing``
version.

Every call site that constructs a ``LicenseClient`` (``proxy/apps/proxy_server/
main.py``, ``services/management/app/api/v1/fleet.py``,
``services/management/app/services/content_filter_deps.py``) wires the
deployment's public host through :func:`apply_deployment_host` so bypass
activates automatically the moment ``requirements.txt``'s pin is bumped to a
release carrying that PR, with zero behaviour change under the version
pinned today.
"""

import os
from collections.abc import Sequence
from typing import Any

# Populated by the Helm chart (k8s/helm/waddleai/templates/_helpers.tpl's
# waddleai.publicHost, wired into both the management and proxy Deployments)
# from the same ingress/HTTPRoute host already used to route this
# deployment's external traffic -- never invented independently of that
# config.
_PUBLIC_HOST_ENV_VAR = "WADDLEAI_PUBLIC_HOST"

# WaddleAI's own PenguinTech-operated product domain (penguintech.md License
# Bypass Domains / penguintech-reference skill Product Domains) -- a managed
# deployment on this domain is billed and operated by PenguinTech directly,
# same as *.penguincloud.io/*.penguintech.cloud.
_PRODUCT_DOMAIN = "waddleai.app"


def resolve_deployment_host() -> str | None:
    """Return this deployment's public hostname, or None if unset.

    Reads ``WADDLEAI_PUBLIC_HOST``, blank/unset in any environment (e.g. a
    bare ``helm template`` render with no ingress mechanism enabled) that
    has no notion of its own public host yet.
    """
    host = os.environ.get(_PUBLIC_HOST_ENV_VAR, "").strip()
    return host or None


def apply_deployment_host(
    client: Any,
    host: str | None = None,
    extra_domains: Sequence[str] = (_PRODUCT_DOMAIN,),
) -> None:
    """Wire a deployment host + product domain into a ``LicenseClient``.

    A no-op against the currently-pinned ``penguin-licensing`` version --
    it has neither ``set_deployment_host`` nor ``set_extra_bypass_domains``
    yet, so domain-based bypass stays inert and every call behaves exactly
    as it does today. Once the pin is bumped to a release carrying
    penguin-libs#83, this activates bypass with no further code change at
    any of the three call sites.
    """
    set_host = getattr(client, "set_deployment_host", None)
    if callable(set_host):
        set_host(host if host is not None else resolve_deployment_host())

    set_extra_domains = getattr(client, "set_extra_bypass_domains", None)
    if callable(set_extra_domains):
        set_extra_domains(extra_domains)


__all__ = ["apply_deployment_host", "resolve_deployment_host"]
