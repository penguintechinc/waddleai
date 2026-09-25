"""Graph extractors for penguincode's three logical graphs (T11-T13).

Each submodule owns one `graph_kind` and writes through the shared
`penguincode_cli.stores.graph.GraphStore` under the caller's `ScopeContext`:
`code.py` (T11, tree-sitter), `knowledge.py` (T12, LLM entity/relation
extraction over indexed docs), `memory.py` (T13, LLM extraction over memory
writes). Deliberately no re-exports here -- these three tasks land in
parallel and each adds its own extractor without touching this file, keeping
merges trivial; import directly from the submodule (e.g. `from
penguincode_cli.graphs.knowledge import extract_knowledge`).
"""
