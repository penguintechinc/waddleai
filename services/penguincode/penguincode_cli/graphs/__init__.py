"""Graph extractors populating the three logical graphs (`graph_kind`).

Each submodule owns one extractor: `code.py` (T11, tree-sitter), `knowledge.py`
(T12, LLM entity/relation extraction over indexed docs), `memory.py` (T13, LLM
extraction over memory writes). Deliberately no re-exports here -- these three
tasks land in parallel and each adds its own extractor without touching this
file, keeping merges trivial; import directly from the submodule
(`from penguincode_cli.graphs.knowledge import extract_knowledge`).
"""
