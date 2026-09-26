"""Post-process ``grpc_tools.protoc`` output into this repo's committed shape.

Invoked by ``make proto`` (see the Makefile) right after protoc generates
``*_pb2.py``/``*_pb2_grpc.py`` and before ``ruff check --fix``/``ruff
format`` run over the same tree. Two independent fixups, each addressing a
gap between what protoc emits and what a Python *package* (as opposed to a
flat script directory) needs:

1. **``*_pb2_grpc.py`` self-imports are never package-relative.** protoc has
   no notion of the enclosing Python package, so it emits either a bare
   ``import X_pb2 as X__pb2`` (flat layout, e.g. ``penguincode.proto``) or a
   ``from <proto-relative-dir> import X_pb2 as ...`` (nested layout, e.g.
   ``knowledge/v1/knowledge.proto``) for its own message module -- both
   forms are rewritten to ``from . import X_pb2 as ...``, since a
   ``*_pb2_grpc.py``'s own message module always lives alongside it in the
   same directory.
2. **``*_pb2.py`` files depending on another ``.proto`` (e.g. a well-known
   type like ``google/protobuf/struct.proto``) get an import ruff's F401
   flags as unused and deletes under ``--fix``.** The alias is referenced
   only by the descriptor pool's own import graph when
   ``AddSerializedFile`` runs -- never by name elsewhere in the file --
   which is a real, load-bearing side effect (registers the dependency
   before this file's serialized descriptor references it), not dead code.
   protoc also places this import after its own runtime imports rather than
   in the top-of-file block, which trips ruff's E402 on top of F401. Both
   are fixed here: the import is relocated into the top import block and
   tagged ``# noqa: F401``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: Matches a `*_pb2_grpc.py` self-import in either shape protoc emits.
_GRPC_IMPORT_RE = re.compile(
    r"^(?:"
    r"import (?P<flat>[a-zA-Z0-9_]+_pb2) as (?P<flat_alias>[a-zA-Z0-9_]+)"
    r"|"
    r"from (?P<pkg>[a-zA-Z0-9_.]+) import (?P<nested>[a-zA-Z0-9_]+_pb2) as (?P<nested_alias>[a-zA-Z0-9_]+)"
    r")$"
)

#: Matches a `*_pb2.py` cross-proto dependency import -- protoc's naming
#: convention always suffixes these aliases with a double-underscore `pb2`
#: (e.g. `google_dot_protobuf_dot_struct__pb2`), distinct from the
#: single-underscore-prefixed runtime imports (`_descriptor`, `_builder`, ...).
_DEP_IMPORT_RE = re.compile(
    r"^from (?P<module>[a-zA-Z0-9_.]+) import (?P<name>[a-zA-Z0-9_]+) as (?P<alias>[a-zA-Z0-9_]+__pb2)$"
)

#: The last line of protoc's always-present runtime-import block -- dependency
#: imports (fixup 2) are inserted immediately after this anchor line.
_RUNTIME_IMPORT_ANCHOR = "from google.protobuf.internal import builder as _builder"


def fix_grpc_imports(text: str) -> str:
    """Rewrite a ``*_pb2_grpc.py``'s own-module import to be package-relative."""
    out: list[str] = []
    for line in text.splitlines():
        match = _GRPC_IMPORT_RE.match(line)
        if match is None:
            out.append(line)
            continue
        if match.group("flat"):
            out.append(f"from . import {match.group('flat')} as {match.group('flat_alias')}")
        else:
            out.append(f"from . import {match.group('nested')} as {match.group('nested_alias')}")
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def fix_pb2_dependency_imports(text: str) -> str:
    """Relocate + ``noqa``-tag any cross-proto dependency import in a ``*_pb2.py``."""
    dep_lines: list[str] = []
    kept: list[str] = []
    for line in text.splitlines():
        match = _DEP_IMPORT_RE.match(line)
        if match is not None:
            dep_lines.append(f"{line}  # noqa: F401")
            continue
        kept.append(line)

    if not dep_lines:
        return text

    out: list[str] = []
    inserted = False
    for line in kept:
        out.append(line)
        if not inserted and line == _RUNTIME_IMPORT_ANCHOR:
            out.extend(dep_lines)
            inserted = True

    if not inserted:
        raise RuntimeError(
            "fix_proto_codegen: could not find the protobuf runtime import anchor "
            f"({_RUNTIME_IMPORT_ANCHOR!r}) to relocate dependency imports next to"
        )

    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def main(argv: list[str]) -> int:
    """Apply both fixups to every generated proto file under ``argv[0]``."""
    if not argv:
        print("usage: fix_proto_codegen.py <proto-dir>", file=sys.stderr)
        return 2

    proto_dir = Path(argv[0])
    for path in sorted(proto_dir.rglob("*_pb2_grpc.py")):
        path.write_text(fix_grpc_imports(path.read_text()))
    for path in sorted(proto_dir.rglob("*_pb2.py")):
        path.write_text(fix_pb2_dependency_imports(path.read_text()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
