"""Backend registry/factory — single chokepoint for building fleet backends.

Maps a ``fleet_backends.type`` value to its concrete ``InferenceFleetBackend``
class and constructs a live instance from a DB row: decrypts
``credentials_ref`` via the provider-credential pattern
(``shared.security.credential_encryption``), passes through ``config``, and
applies the row's ``management_scope``. Callers should never branch on
``type`` themselves — always go through ``build_backend``.
"""

import importlib
import logging
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from shared.fleet.base import BackendType, InferenceFleetBackend, ManagementScope
from shared.security.credential_encryption import decrypt_credential

logger = logging.getLogger(__name__)

_BackendT = TypeVar("_BackendT", bound=InferenceFleetBackend)

# type -> concrete class constructor, populated by @register() decorators on
# each backend module. Concrete backends take different __init__ kwargs
# beyond the (db, config, credentials) contract build_backend relies on, so
# this is typed as a Callable rather than type[InferenceFleetBackend].
_REGISTRY: dict[BackendType, Callable[..., InferenceFleetBackend]] = {}

# Owning module for each backend type, imported on first lookup so a
# missing optional SDK only breaks that one backend type, not the registry.
_MODULE_MAP: dict[BackendType, str] = {
    BackendType.OLLAMA: "services.management.app.services.ollama_manager",
    BackendType.LLAMACPP: "services.management.app.services.llamacpp_manager",
    BackendType.EXO: "shared.fleet.exo",
    BackendType.VERTEX_AI: "shared.fleet.vertex_ai",
    BackendType.BEDROCK: "shared.fleet.bedrock",
}


class FleetBackendRow(Protocol):
    """Structural type for a ``fleet_backends`` row (ORM row or PyDAL Row)."""

    id: int
    type: str
    management_scope: str
    config: dict[str, Any] | None
    credentials_ref: str | None


def register(backend_type: BackendType) -> Callable[[type[_BackendT]], type[_BackendT]]:
    """Class decorator registering a concrete backend under ``backend_type``."""

    def decorator(cls: type[_BackendT]) -> type[_BackendT]:
        _REGISTRY[backend_type] = cls
        return cls

    return decorator


def _mask_secret(secret: str) -> str:
    """Mask a decrypted secret for logging — never the plaintext value."""
    if len(secret) <= 4:
        return "*" * len(secret)
    return f"{'*' * (len(secret) - 4)}{secret[-4:]}"


def _ensure_imported(backend_type: BackendType) -> None:
    """Import the module that owns ``backend_type`` so its @register runs."""
    if backend_type in _REGISTRY:
        return
    module_name = _MODULE_MAP.get(backend_type)
    if module_name is None:
        return
    try:
        importlib.import_module(module_name)
    except ImportError as exc:
        logger.warning(
            "Fleet backend module %s unavailable for type %s: %s",
            module_name,
            backend_type.value,
            exc,
        )


def build_backend(db: Any, row: FleetBackendRow) -> InferenceFleetBackend:
    """Construct the concrete ``InferenceFleetBackend`` for a ``fleet_backends`` row.

    Resolves ``config``/``credentials_ref`` and applies ``management_scope``
    from the row. Raises ``ValueError`` for an unknown or unregistered type.
    """
    try:
        backend_type = BackendType(row.type)
    except ValueError as exc:
        raise ValueError(f"Unknown fleet backend type: {row.type!r}") from exc

    _ensure_imported(backend_type)
    backend_cls = _REGISTRY.get(backend_type)
    if backend_cls is None:
        raise ValueError(f"No backend implementation registered for type: {backend_type.value}")

    credentials: str | None = None
    if row.credentials_ref:
        credentials = decrypt_credential(row.credentials_ref)
        logger.debug(
            "Resolved credentials for fleet backend id=%s type=%s: %s",
            row.id,
            backend_type.value,
            _mask_secret(credentials),
        )

    backend = backend_cls(db=db, config=row.config or {}, credentials=credentials)
    backend.management_scope = ManagementScope(row.management_scope)
    backend.fleet_backend_id = row.id
    return backend
