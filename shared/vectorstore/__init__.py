"""Generic vector-store interface for the local-only profile (spec §17).

``VectorStoreBackend`` is the pluggable seam two implementations satisfy —
``PgvectorVectorStore`` (default, cluster path) and ``QdrantVectorStore``
(local-only profile, off by default) — so callers select a backend via
config/feature-flag instead of branching on backend type themselves.
Modeled on ``shared.fleet``'s ``InferenceFleetBackend`` ABC + registry.
"""
