#!/usr/bin/env python3
"""Export the pooled-quota Lua scripts to rust/crates/quota/lua/*.lua byte-for-byte.

Python (`shared/quota/lease.py`) stays the single source of the script text; the Rust `quota`
crate embeds the exported files via `include_str!` and a drift test re-runs this exporter.
  usage: export-quota-lua.py [--out DIR] [--check]
  --out DIR  write to DIR instead of rust/crates/quota/lua (used by the drift test)
  --check    write nothing; exit 1 when the committed files differ from the Python constants
The constants are read via `ast` (no imports), so no third-party deps are needed.
"""

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "shared/quota/lease.py"
DEFAULT_OUT = ROOT / "rust/crates/quota/lua"
NAMES = {"LUA_RESERVE": "reserve.lua", "LUA_RECONCILE": "reconcile.lua"}


def extract() -> dict[str, str]:
    """Return {constant: exact string value} for the module-level quota Lua constants."""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name) and tgt.id in NAMES:
                    if not isinstance(stmt.value.value, str) or not stmt.value.value:
                        raise SystemExit(f"{tgt.id} in {SRC} is not a non-empty string")
                    found[tgt.id] = stmt.value.value
    missing = set(NAMES) - set(found)
    if missing:
        raise SystemExit(f"missing constants in {SRC}: {sorted(missing)}")
    return found


def main() -> int:
    """Write (or with --check, verify) the exported Lua files; returns the exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    scripts = extract()
    drift = False
    for const, fname in NAMES.items():
        path = args.out / fname
        data = scripts[const].encode("utf-8")
        if args.check:
            if not path.exists() or path.read_bytes() != data:
                print(f"DRIFT: {path} differs from {const}", file=sys.stderr)
                drift = True
        else:
            args.out.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            print(f"wrote {path} ({len(data)} bytes)")
    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
