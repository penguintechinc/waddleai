"""Graph extractors for penguincode's three logical graphs (T11-T13).

Each submodule owns one `graph_kind` and writes through the shared
`penguincode_cli.stores.graph.GraphStore` under the caller's `ScopeContext`:
`code.py` (T11, tree-sitter), `knowledge.py` (T12, LLM), `memory.py` (T13, LLM).
"""
