"""Auto Memory / Knowledge Layer (§9): CodeRAG, docs cache, ingestion, scoping.

Shared primitives used by both the Management service (CodeRAG worker, docs
cache, knowledge ingestion API) and the AIProxy (knowledge retrieval/
injection pipeline stage). Submodules are imported directly
(``shared.knowledge.embed``, ``shared.knowledge.scoping``, ...) rather than
re-exported here, matching the ``shared.utils`` package convention.
"""
