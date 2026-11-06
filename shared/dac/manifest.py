"""Strict, side-effect-free DAC v1alpha1 parsing and planning (spec §20)."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import yaml
from yaml.events import AliasEvent

API_VERSION = "waddleai.penguintech.io/v1alpha1"
_TOP_LEVEL = {"apiVersion", "kind", "metadata", "spec"}
_SPEC_FIELDS = {"backends", "mcp_servers", "role_models", "hook_rules"}
_SECRET_PATTERN = re.compile(
    r"token|secret|password|passwd|apikey|auth|credential|bearer|privatekey|key$"
)
# Exact normalised schema fields that match the pattern but are not secrets
# (none exist in v1alpha1; add here with justification rather than weakening it).
_SAFE_KEYS: frozenset[str] = frozenset()
_REDACTED = "***REDACTED***"
MAX_RAW_BYTES = 1024 * 1024
MAX_NODES = 50_000
MAX_DEPTH = 64


class DacError(ValueError):
    """Raised when a configuration-as-code manifest is invalid or unsafe.

    Covers unknown fields, inline secrets, pruning and cross-org references.
    """

    pass


@dataclass(frozen=True, slots=True)
class DacPlanItem:
    """A single planned change (create/update/no-op) for one manifest resource.

    Part of a DacPlan that reviewers inspect before apply.
    """

    resource_type: str
    name: str
    action: str
    desired: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DacPlan:
    """A deterministic, digestable plan produced from a manifest.

    Apply must present this exact plan, unexpired, to take effect.
    """

    id: str
    manifest_digest: str
    organization_id: str
    items: tuple[DacPlanItem, ...]
    expires_at: float


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _is_secret_key(key: Any) -> bool:
    """Return True when a key, normalised (lowercase, no ``-``/``_``), looks like a secret.

    Normalising defeats ``HF-Token`` / ``API_Key`` style evasion of exact matching.
    """
    norm = str(key).lower().replace("-", "").replace("_", "")
    return norm not in _SAFE_KEYS and _SECRET_PATTERN.search(norm) is not None


def _is_secret_pair(value: dict[Any, Any]) -> bool:
    """Return True for an env-style ``{name, value}`` pair whose name looks secret."""
    return "value" in value and "name" in value and _is_secret_key(value["name"])


def _reject_secrets(value: Any, path: str = "") -> None:
    """Raise DacError if any key or env-style pair name looks like an inline secret."""
    if isinstance(value, dict):
        if _is_secret_pair(value):
            raise DacError(f"inline secret field rejected: {path}{value['name']}")
        for key, nested in value.items():
            if _is_secret_key(key):
                raise DacError(f"inline secret field rejected: {path}{key}")
            _reject_secrets(nested, f"{path}{key}.")
    elif isinstance(value, list):
        for nested in value:
            _reject_secrets(nested, path)


def redact_secrets(value: Any) -> Any:
    """Return a deep copy with secret-looking values masked.

    Defence in depth for persisted revisions: even if rejection is ever bypassed,
    the stored ``manifest_redacted`` never carries the secret value.
    """
    if isinstance(value, dict):
        pair = _is_secret_pair(value)
        return {
            k: (_REDACTED if (_is_secret_key(k) or (pair and k == "value")) else redact_secrets(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(v) for v in value]
    return value


class _NoAliasLoader(yaml.SafeLoader):
    """SafeLoader that refuses anchors and aliases, preventing billion-laughs expansion."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        """Raise DacError on any alias or anchor event before composing the node."""
        if self.check_event(AliasEvent):
            raise DacError("YAML aliases are not allowed")
        event = self.peek_event()
        if getattr(event, "anchor", None) is not None:
            raise DacError("YAML anchors are not allowed")
        return super().compose_node(parent, index)


def _check_limits(value: Any) -> None:
    """Iteratively enforce node-count and depth caps on a parsed document."""
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES:
            raise DacError(f"manifest exceeds {MAX_NODES} nodes")
        if depth > MAX_DEPTH:
            raise DacError(f"manifest nesting too deep (>{MAX_DEPTH})")
        if isinstance(item, dict):
            for k, v in item.items():
                stack.append((k, depth + 1))
                stack.append((v, depth + 1))
        elif isinstance(item, (list, tuple)):
            stack.extend((v, depth + 1) for v in item)


def _load_document(raw: str | bytes | dict[str, Any]) -> Any:
    """Load a manifest from YAML text or dict, enforcing size/alias/depth/node caps."""
    if isinstance(raw, (str, bytes)):
        size = len(raw.encode()) if isinstance(raw, str) else len(raw)
        if size > MAX_RAW_BYTES:
            raise DacError("manifest too large")
        try:
            document = yaml.load(raw, Loader=_NoAliasLoader)  # noqa: S506  # nosec B506
        except RecursionError as exc:
            raise DacError(f"manifest nesting too deep (>{MAX_DEPTH})") from exc
        except yaml.YAMLError as exc:
            raise DacError(f"invalid YAML: {exc}") from exc
    else:
        document = raw
    _check_limits(document)
    return document


def parse_manifest(raw: str | bytes | dict[str, Any], *, organization_id: str) -> dict[str, Any]:
    """Parse and strictly validate a v1alpha1 manifest without mutating state."""
    document = _load_document(raw)
    if not isinstance(document, dict) or set(document) - _TOP_LEVEL:
        raise DacError("manifest has unknown top-level fields")
    if document.get("apiVersion") != API_VERSION or document.get("kind") != "WaddleAIConfiguration":
        raise DacError("unsupported apiVersion or kind")
    metadata = document.get("metadata")
    spec = document.get("spec")
    if (
        not isinstance(metadata, dict)
        or not isinstance(metadata.get("name"), str)
        or not metadata["name"]
    ):
        raise DacError("metadata.name is required")
    if "organization" in metadata and str(metadata["organization"]) != str(organization_id):
        raise DacError("metadata.organization does not match authenticated organization")
    if not isinstance(spec, dict) or set(spec) - _SPEC_FIELDS:
        raise DacError("manifest spec has unknown fields")
    if "prune" in document or "prune" in spec:
        raise DacError("prune is not available in v1alpha1")
    _reject_secrets(document)
    for collection in ("backends", "mcp_servers", "hook_rules"):
        if collection in spec and not isinstance(spec[collection], list):
            raise DacError(f"spec.{collection} must be a list")
    return document


class DacPlanner:
    """Creates deterministic create/update/no-op plans from a current-state lookup."""

    def __init__(self, current: Callable[[str, str], dict[str, Any] | None]) -> None:
        """Store the callable used to look up a resource's current state.

        Injected so planning stays side-effect free and testable.
        """
        self._current = current

    def plan(
        self, manifest: dict[str, Any], *, organization_id: str, now: float | None = None
    ) -> DacPlan:
        """Compute a deterministic plan of changes for a manifest.

        Read-only: it never mutates state, so dry runs are safe.
        """
        validated = parse_manifest(manifest, organization_id=organization_id)
        items: list[DacPlanItem] = []
        for resource_type, field in (
            ("backend", "backends"),
            ("mcp_server", "mcp_servers"),
            ("hook_rule", "hook_rules"),
        ):
            for desired in validated["spec"].get(field, []):
                if not isinstance(desired, dict) or not isinstance(desired.get("name"), str):
                    raise DacError(f"{field} entries require a name")
                name = desired["name"]
                current = self._current(resource_type, name)
                action = (
                    "create"
                    if current is None
                    else ("no-op" if _canonical(current) == _canonical(desired) else "update")
                )
                items.append(DacPlanItem(resource_type, name, action, desired))
        digest = hashlib.sha256(_canonical(validated).encode()).hexdigest()
        created = time.time() if now is None else now
        return DacPlan(
            secrets.token_urlsafe(18), digest, str(organization_id), tuple(items), created + 600
        )


class DacPlanStore:
    """Short-lived reviewed-plan store; executor is injected by the Management API."""

    def __init__(self) -> None:
        """Initialise an empty in-memory plan store.

        Holds reviewed plans keyed by id until they are applied.
        """
        self._plans: dict[str, DacPlan] = {}

    def put(self, plan: DacPlan) -> DacPlan:
        """Store a reviewed plan and return it.

        Makes the plan available for a later exact-match apply.
        """
        self._plans[plan.id] = plan
        return plan

    def has(self, plan_id: str) -> bool:
        """Whether this process has a reviewed plan cached locally."""
        return plan_id in self._plans

    def apply(
        self,
        plan_id: str,
        *,
        organization_id: str,
        executor: Callable[[DacPlanItem], None],
        now: float | None = None,
    ) -> tuple[DacPlanItem, ...]:
        """Apply a stored plan after verifying it matches and has not expired.

        Enforces the review-before-apply contract.
        """
        plan = self._plans.get(plan_id)
        if plan is None or plan.organization_id != str(organization_id):
            raise DacError("unknown plan")
        if (time.time() if now is None else now) > plan.expires_at:
            self._plans.pop(plan_id, None)
            raise DacError("stale plan; generate a new dry-run")
        for item in plan.items:
            if item.action != "no-op":
                executor(item)
        self._plans.pop(plan_id, None)
        return plan.items
