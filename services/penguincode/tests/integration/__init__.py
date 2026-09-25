"""T16: end-to-end integration suite for the penguincode knowledge platform.

Unlike every other `tests/test_*.py` module (servicer-direct calls against a
fake `grpc.aio.ServicerContext`, or module-level calls against a live
Postgres), this package drives a **real** `grpc.aio.server` -- the actual
`KnowledgeServiceImpl` + `WaddleAIAuthInterceptor` wiring -- over the wire,
through the real `KnowledgeClient`, against a real pgvector Postgres. It is
the first place RS256 JWT validation, gRPC serialization, and the full
index/query/memory/code-graph pipelines are all exercised together.
"""
