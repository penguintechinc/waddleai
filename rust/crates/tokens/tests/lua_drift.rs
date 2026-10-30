//! Drift gate: the embedded Lua must equal the Python `TokenLimiter` constants byte-for-byte.

use std::path::PathBuf;

fn python_constant(src: &str, name: &str) -> String {
    let marker = format!("{name} = \"\"\"");
    let start = src
        .find(&marker)
        .unwrap_or_else(|| panic!("{name} not found"))
        + marker.len();
    let end = src[start..].find("\"\"\"").expect("unterminated constant") + start;
    src[start..end].to_string()
}

fn python_source() -> String {
    let p =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../shared/utils/token_limiter.py");
    std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("cannot read {}: {e}", p.display()))
}

#[test]
fn embedded_reserve_lua_equals_python() {
    let py = python_constant(&python_source(), "LUA_RESERVE");
    assert!(!py.is_empty());
    assert_eq!(tokens::LUA_RESERVE.as_bytes(), py.as_bytes());
}

#[test]
fn embedded_reconcile_lua_equals_python() {
    let py = python_constant(&python_source(), "LUA_RECONCILE");
    assert!(!py.is_empty());
    assert_eq!(tokens::LUA_RECONCILE.as_bytes(), py.as_bytes());
}
