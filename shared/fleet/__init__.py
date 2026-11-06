"""Inference fleet backend package.

Exposes the pluggable ``InferenceFleetBackend`` interface (§10.1 of the
platform spec) plus the value types every backend exchanges with callers.
Concrete backends (Ollama, llama.cpp, EXO, Vertex AI, Bedrock, Azure AI, and
Ollama Cloud) live in sibling modules and register themselves with
``shared.fleet.registry``.
"""

from shared.fleet.base import (
    BackendType,
    Endpoint,
    FleetHealth,
    InferenceFleetBackend,
    ManagementScope,
    ModelPlacement,
    NodeInfo,
    ProvisionSpec,
)
from shared.fleet.idle_teardown import IdleTeardownController

__all__ = [
    "BackendType",
    "Endpoint",
    "FleetHealth",
    "InferenceFleetBackend",
    "IdleTeardownController",
    "ManagementScope",
    "ModelPlacement",
    "NodeInfo",
    "ProvisionSpec",
]
