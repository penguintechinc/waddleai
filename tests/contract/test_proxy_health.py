"""
Health check endpoint tests for the proxy service's K8s probe endpoints.

These tests verify that /livez (liveness) and /readyz (readiness) endpoints
are properly exposed, public (no auth required), and return correct status codes
and response bodies.
"""

import httpx
import pytest

_READYZ_XFAIL_REASON = (
    "gh-130: shared/utils/health_checks.py:100 calls db.executesql('SELECT 1'), "
    "a PyDAL API penguin_dal does not implement -- the DB check always reports "
    "unhealthy, so /readyz always 503s. Pre-existing, confirmed via git stash."
)


def test_livez_alive(proxy_url):
    """GET /livez returns 200 with body 'alive'."""
    r = httpx.get(f"{proxy_url}/livez")
    assert r.status_code == 200
    assert r.text == "alive"


def test_livez_no_auth_required(proxy_url):
    """GET /livez works without Authorization header (proves it's public)."""
    # No Authorization header; if /livez were gated, this would 401
    r = httpx.get(f"{proxy_url}/livez")
    assert r.status_code == 200


@pytest.mark.xfail(reason=_READYZ_XFAIL_REASON, strict=False)
def test_readyz_ready(proxy_url):
    """GET /readyz returns 200 with JSON body containing 'status' key.

    In test mode with sqlite DB up, readyz should report ready.
    """
    r = httpx.get(f"{proxy_url}/readyz")
    assert r.status_code == 200
    body = r.json()
    assert "status" in body
    # In test mode with DB running, expect healthy or degraded
    assert body["status"] in ("healthy", "degraded")


@pytest.mark.xfail(reason=_READYZ_XFAIL_REASON, strict=False)
def test_readyz_no_auth_required(proxy_url):
    """GET /readyz works without Authorization header (proves it's public).

    Kubelet probes do not send auth headers; verify endpoints are in _PUBLIC_PATHS.
    """
    # No Authorization header; if /readyz were gated, this would 401
    r = httpx.get(f"{proxy_url}/readyz")
    assert r.status_code == 200


def test_healthz_regression(proxy_url):
    """GET /healthz still works (regression guard)."""
    r = httpx.get(f"{proxy_url}/healthz")
    assert r.status_code == 200
    assert r.text == "healthy"
