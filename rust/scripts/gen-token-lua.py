#!/usr/bin/env python3
"""Export TokenLimiter's Lua scripts to rust/crates/tokens/lua/*.lua byte-for-byte.

Python (`shared/utils/token_limiter.py`) stays the single source of the script text; the Rust
crate embeds the exported files via `include_str!`.
  usage: gen-token-lua.py [--check]   (--check exits 1 on drift)
The class constants are read via `ast` (no imports), so no third-party deps are needed.
"""

import ast
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
SRC = root / "shared/utils/token_limiter.py"
OUT = root / "rust/crates/tokens/lua"
NAMES = {"LUA_RESERVE": "reserve.lua", "LUA_RECONCILE": "reconcile.lua"}


def extract() -> dict[str, str]:
    """Return {constant: exact string value} for the TokenLimiter Lua constants."""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TokenLimiter"):
        for stmt in cls.body:
            if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
                for tgt in stmt.targets:
                    if isinstance(tgt, ast.Name) and tgt.id in NAMES:
                        found[tgt.id] = stmt.value.value
    missing = set(NAMES) - set(found)
    if missing:
        raise SystemExit(f"missing constants in {SRC}: {sorted(missing)}")
    return found


def main() -> int:
    """Write (or with --check, verify) the exported Lua files."""
    scripts = extract()
    drift = False
    for const, fname in NAMES.items():
        path = OUT / fname
        if "--check" in sys.argv:
            if not path.exists() or path.read_bytes() != scripts[const].encode("utf-8"):
                print(f"DRIFT: {path} differs from {const}", file=sys.stderr)
                drift = True
        else:
            OUT.mkdir(parents=True, exist_ok=True)
            path.write_bytes(scripts[const].encode("utf-8"))
            print(f"wrote {path} ({len(scripts[const])} bytes)")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
