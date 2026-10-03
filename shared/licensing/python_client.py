"""PenguinTech License Server Python Client.

This module provides a Python client for integrating with the PenguinTech License Server
to validate licenses and check feature entitlements.

``check_feature`` implements stale-while-error: once the normal 5-minute
cache TTL expires, a license-server outage serves the last-known entitlement
(regardless of that TTL) rather than hard-denying, up to
``LICENSE_MAX_STALE_SECONDS`` old (default 7 days) -- a license-server
outage must never lock out an already-entitled, paying tenant. Only a
feature that was never successfully fetched, or whose cached entry has aged
past the max-stale window, hard-denies. Kill switch:
``waddleai.disable-license-stale-cache`` (unseen/OFF, the default, keeps
this mechanism ON; ON reverts to the pre-fix behaviour of hard-denying on
any request exception).
"""

import logging
import os
import time
from functools import wraps
from typing import Any, Optional

import requests

from shared.utils.feature_flags import is_feature_enabled

logger = logging.getLogger(__name__)

_DISABLE_STALE_CACHE_FLAG = "waddleai.disable-license-stale-cache"

_DEFAULT_MAX_STALE_SECONDS = 7 * 24 * 60 * 60  # 7 days

_license_checks_counter: Any = None


def _max_stale_seconds() -> float:
    """``LICENSE_MAX_STALE_SECONDS`` env override, default 7 days."""
    raw = os.getenv("LICENSE_MAX_STALE_SECONDS")
    if raw is None:
        return float(_DEFAULT_MAX_STALE_SECONDS)
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "Invalid LICENSE_MAX_STALE_SECONDS=%r, using default=%s",
            raw,
            _DEFAULT_MAX_STALE_SECONDS,
        )
        return float(_DEFAULT_MAX_STALE_SECONDS)


def _record_license_check(result: str) -> None:
    """Increment ``license_checks_total{result=...}``. Never raises."""
    global _license_checks_counter
    try:
        if _license_checks_counter is None:
            from shared.observability.metrics import get_meter

            _license_checks_counter = get_meter().create_counter(
                "license_checks_total",
                unit="1",
                description=(
                    "License feature-entitlement checks by result: live (fresh "
                    "fetch), stale (served past normal TTL during an outage), "
                    "denied (hard denial), bypass (domain-based, recorded by "
                    "the caller's penguin_licensing/domain_bypass integration)"
                ),
            )
        _license_checks_counter.add(1, {"result": result})
    except Exception as exc:  # noqa: BLE001 -- telemetry must never break licensing
        logger.debug("license check telemetry emission failed: %s", exc)


class FeatureNotAvailableError(Exception):
    """Raised when a required feature is not available in the current license."""

    def __init__(self, feature: str):
        """Record which *feature* was denied and build the error message."""
        self.feature = feature
        super().__init__(f"Feature '{feature}' requires license upgrade")


class LicenseValidationError(Exception):
    """Raised when license validation fails."""

    pass


class PenguinTechLicenseClient:
    """Client for PenguinTech License Server integration."""

    def __init__(
        self,
        license_key: str,
        product: str,
        base_url: str | None = None,
        timeout: int = 30,
    ):
        """Initialize the license client.

        Args:
            license_key: The license key (format: PENG-XXXX-XXXX-XXXX-XXXX-ABCD)
            product: The product identifier
            base_url: License server URL (default: https://license.penguintech.io)
            timeout: Request timeout in seconds

        """
        self.license_key = license_key
        self.product = product
        self.base_url = base_url or "https://license.penguintech.io"
        # Populated by validate(); keepalive() needs it. Annotated so assigning
        # the real id later is not an assignment of str to an inferred None.
        self.server_id: str | None = None
        self.timeout = timeout

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {license_key}",
                "Content-Type": "application/json",
            }
        )
        # No `self.session.timeout = timeout` here: requests.Session accepts the
        # attribute but Session.request() never reads it (verified against
        # requests 2.34.2), so it silently bought nothing and every call to the
        # license server was unbounded -- a hung license.penguintech.io could
        # block the caller indefinitely. The timeout is passed per-request
        # below, which is the only form requests honours.

        # Feature cache. Explicitly annotated: the empty-dict/None initialisers
        # otherwise infer as dict[Any, Any] and None, so every later
        # `self._cache_timestamp = time.time()` reads as assigning float to None.
        self._feature_cache: dict[str, bool] = {}
        self._cache_timestamp: float | None = None
        self._cache_ttl = 300  # 5 minutes

    @classmethod
    def from_env(cls, timeout: int = 30) -> Optional["PenguinTechLicenseClient"]:
        """Create client from environment variables.

        Requires LICENSE_KEY and PRODUCT_NAME environment variables.
        Optional LICENSE_SERVER_URL for custom server.

        Args:
            timeout: Request timeout in seconds

        Returns:
            PenguinTechLicenseClient instance or None if env vars missing

        """
        license_key = os.getenv("LICENSE_KEY")
        product = os.getenv("PRODUCT_NAME")
        base_url = os.getenv("LICENSE_SERVER_URL")

        if not license_key or not product:
            logger.warning("LICENSE_KEY and PRODUCT_NAME environment variables required")
            return None

        return cls(license_key, product, base_url, timeout)

    def validate(self) -> dict[str, Any]:
        """Validate license and get server ID for keepalives.

        Returns:
            Dict containing validation response

        Raises:
            LicenseValidationError: If validation fails

        """
        try:
            response = self.session.post(
                f"{self.base_url}/api/v2/validate",
                json={"product": self.product},
                timeout=self.timeout,
            )
            response.raise_for_status()

            data = response.json()

            if not data.get("valid"):
                raise LicenseValidationError(f"License validation failed: {data.get('message')}")

            # Store server ID for keepalives
            if "metadata" in data and "server_id" in data["metadata"]:
                self.server_id = data["metadata"]["server_id"]

            # Update feature cache
            self._update_feature_cache(data.get("features", []))

            return data

        except requests.RequestException as e:
            raise LicenseValidationError(f"License validation request failed: {e}")  # noqa: B904 -- re-raise without chaining is existing control flow, out of scope for this pass

    def check_feature(self, feature: str, use_cache: bool = True) -> bool:
        """Check if a specific feature is enabled.

        On a license-server outage (``requests.RequestException``), degrades
        to the last-known entitlement for ``feature`` -- see the module
        docstring for the stale-while-error contract and the
        ``waddleai.disable-license-stale-cache`` kill switch.

        Args:
            feature: Feature name to check
            use_cache: Whether to use cached results

        Returns:
            True if feature is enabled, False otherwise

        """
        # Check cache first if enabled and valid
        if use_cache and self._is_cache_valid():
            cached_result = self._feature_cache.get(feature)
            if cached_result is not None:
                _record_license_check("live")
                return cached_result

        try:
            response = self.session.post(
                f"{self.base_url}/api/v2/features",
                json={"product": self.product, "feature": feature},
                timeout=self.timeout,
            )
            response.raise_for_status()

            data = response.json()
            features = data.get("features", [])

            if features:
                entitled = features[0].get("entitled", False)
                # Cache the result
                self._feature_cache[feature] = entitled
                self._cache_timestamp = time.time()
                _record_license_check("live")
                return entitled

            _record_license_check("live")
            return False

        except requests.RequestException as e:
            if is_feature_enabled(_DISABLE_STALE_CACHE_FLAG, default=False):
                # Kill switch ON: pre-fix behaviour, hard-deny, no stale serving.
                logger.error(f"Feature check failed for {feature}: {e}")
                _record_license_check("denied")
                return False
            return self._serve_stale_or_deny(feature, e)

    def _serve_stale_or_deny(self, feature: str, error: Exception) -> bool:
        """Stale-while-error fallback for :meth:`check_feature`.

        Serves the last-known entitlement for ``feature`` -- regardless of
        the normal 5-minute cache TTL -- as long as it is younger than
        ``LICENSE_MAX_STALE_SECONDS``. Hard-denies only when ``feature`` was
        never successfully fetched, or its cached entry has aged past that
        window; a license-server outage must never lock out an
        already-entitled, paying tenant.
        """
        cached_result = self._feature_cache.get(feature)
        if cached_result is not None and self._cache_timestamp is not None:
            age = time.time() - self._cache_timestamp
            max_stale = _max_stale_seconds()
            if age < max_stale:
                logger.warning(
                    "License server unreachable checking feature %s (%s); serving "
                    "stale cached entitlement=%s (age=%.0fs, max_stale=%.0fs)",
                    feature,
                    error,
                    cached_result,
                    age,
                    max_stale,
                )
                _record_license_check("stale")
                return cached_result
            logger.error(
                "License server unreachable checking feature %s (%s); cached "
                "entitlement is %.0fs old, past LICENSE_MAX_STALE_SECONDS=%.0fs -- denying",
                feature,
                error,
                age,
                max_stale,
            )
            _record_license_check("denied")
            return False

        logger.error(f"Feature check failed for {feature} and no prior successful fetch: {error}")
        _record_license_check("denied")
        return False

    def keepalive(self, usage_data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send keepalive with optional usage statistics.

        Args:
            usage_data: Optional usage statistics to send

        Returns:
            Dict containing keepalive response

        Raises:
            LicenseValidationError: If keepalive fails

        """
        if not self.server_id:
            # Validate first to get server ID
            validation = self.validate()
            if not validation.get("valid"):
                raise LicenseValidationError("Failed to validate license for keepalive")

        payload = {"product": self.product, "server_id": self.server_id}

        if usage_data:
            payload.update(usage_data)

        try:
            response = self.session.post(
                f"{self.base_url}/api/v2/keepalive", json=payload, timeout=self.timeout
            )
            response.raise_for_status()

            return response.json()

        except requests.RequestException as e:
            raise LicenseValidationError(f"Keepalive request failed: {e}")  # noqa: B904 -- re-raise without chaining is existing control flow, out of scope for this pass

    def get_all_features(self) -> dict[str, bool]:
        """Get all available features from cache or validation.

        Returns:
            Dict mapping feature names to enabled status

        """
        if not self._is_cache_valid():
            try:
                self.validate()
            except LicenseValidationError:
                logger.error("Failed to refresh feature cache")

        return self._feature_cache.copy()

    def _update_feature_cache(self, features: list[dict[str, Any]]) -> None:
        """Update the feature cache with new feature data."""
        self._feature_cache = {}
        for feature in features:
            name = feature.get("name")
            entitled = feature.get("entitled", False)
            if name:
                self._feature_cache[name] = entitled

        self._cache_timestamp = time.time()

    def _is_cache_valid(self) -> bool:
        """Check if the feature cache is still valid."""
        if self._cache_timestamp is None:
            return False

        return (time.time() - self._cache_timestamp) < self._cache_ttl

    @staticmethod
    def is_valid_license_key(key: str) -> bool:
        """Validate license key format.

        Args:
            key: License key to validate

        Returns:
            True if format is valid

        """
        if not key or len(key) != 29:
            return False

        if not key.startswith("PENG-"):
            return False

        # Count dashes - should be 5 total
        return key.count("-") == 5


# Global client instance for convenience
_global_client: PenguinTechLicenseClient | None = None


def get_client() -> PenguinTechLicenseClient | None:
    """Get the global license client instance."""
    global _global_client
    if _global_client is None:
        _global_client = PenguinTechLicenseClient.from_env()
    return _global_client


def requires_feature(feature_name: str, client: PenguinTechLicenseClient | None = None):
    """Decorator to gate functionality behind license features.

    Args:
        feature_name: Name of the required feature
        client: License client instance (uses global if None)

    Raises:
        FeatureNotAvailableError: If feature is not available

    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            license_client = client or get_client()

            if not license_client:
                raise FeatureNotAvailableError(feature_name)

            if not license_client.check_feature(feature_name):
                raise FeatureNotAvailableError(feature_name)

            return func(*args, **kwargs)

        return wrapper

    return decorator


def initialize_licensing(
    license_key: str | None = None, product: str | None = None
) -> dict[str, Any]:
    """Initialize licensing system and validate license.

    Args:
        license_key: License key (uses env var if None)
        product: Product name (uses env var if None)

    Returns:
        Validation response dict

    Raises:
        LicenseValidationError: If initialization fails

    """
    global _global_client

    # Use provided values or environment variables
    resolved_key = license_key or os.getenv("LICENSE_KEY")
    resolved_product = product or os.getenv("PRODUCT_NAME")

    if not resolved_key or not resolved_product:
        raise LicenseValidationError("LICENSE_KEY and PRODUCT_NAME are required")

    _global_client = PenguinTechLicenseClient(resolved_key, resolved_product)
    validation = _global_client.validate()

    logger.info(f"License valid for {validation['customer']} ({validation['tier']} tier)")

    # Log available features
    for feature in validation.get("features", []):
        if feature.get("entitled"):
            logger.info(f"Feature enabled: {feature['name']}")

    return validation


# Convenience functions for common operations
def check_feature(feature: str) -> bool:
    """Check if a feature is available using the global client."""
    client = get_client()
    if not client:
        return False
    return client.check_feature(feature)


def send_keepalive(usage_data: dict[str, Any] | None = None) -> bool:
    """Send keepalive using the global client."""
    client = get_client()
    if not client:
        return False

    try:
        client.keepalive(usage_data)
        return True
    except LicenseValidationError:
        logger.error("Failed to send keepalive")
        return False
