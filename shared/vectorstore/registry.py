"""Backend registry/factory — single chokepoint for building vector-store backends.

Mirrors ``shared.fleet.registry``: maps a ``VectorStoreBackendType`` to its
concrete ``VectorStoreBackend`` class, importing the owning module lazily so
a missing optional SDK (``qdrant-client``) only breaks that one backend
type, not the registry. Callers that already know which concrete class they
want (e.g. ``shared.vectorstore.factory``, which is profile-aware) may
import it directly instead — this registry exists for callers that select a
backend generically by ``VectorStoreBackendType``.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from typing import TypeVar

from shared.vectorstore.base import VectorStoreBackend, VectorStoreBackendType

logger = logging.getLogger(__name__)

_BackendT = TypeVar("_BackendT", bound=VectorStoreBackend)

_REGISTRY: dict[VectorStoreBackendType, Callable[..., VectorStoreBackend]] = {}

_MODULE_MAP: dict[VectorStoreBackendType, str] = {
    VectorStoreBackendType.PGVECTOR: "shared.vectorstore.pgvector_backend",
    VectorStoreBackendType.QDRANT: "shared.vectorstore.qdrant_backend",
}


def register(
    backend_type: VectorStoreBackendType,
) -> Callable[[type[_BackendT]], type[_BackendT]]:
    """Class decorator registering a concrete backend under ``backend_type``."""

    def decorator(cls: type[_BackendT]) -> type[_BackendT]:
        _REGISTRY[backend_type] = cls
        return cls

    return decorator


def _ensure_imported(backend_type: VectorStoreBackendType) -> None:
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
            "Vector store backend module %s unavailable for type %s: %s",
            module_name,
            backend_type.value,
            exc,
        )


def build_vector_store(
    backend_type: VectorStoreBackendType, **kwargs: object
) -> VectorStoreBackend:
    """Construct the concrete ``VectorStoreBackend`` for ``backend_type``.

    ``**kwargs`` are forwarded verbatim to the concrete class's
    ``__init__`` — backends take different constructor arguments (a
    penguin-dal ``db`` handle for pgvector; a URL/API key/timeout for
    Qdrant), so this stays untyped at the registry boundary. Raises
    ``ValueError`` for an unknown or unregistered type.
    """
    _ensure_imported(backend_type)
    backend_cls = _REGISTRY.get(backend_type)
    if backend_cls is None:
        raise ValueError(f"No vector store backend registered for type: {backend_type.value}")
    return backend_cls(**kwargs)
