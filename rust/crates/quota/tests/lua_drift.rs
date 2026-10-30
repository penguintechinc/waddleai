//! Drift gate: the embedded Lua must equal the Python `LUA_RESERVE`/`LUA_RECONCILE` constants
//! byte-for-byte. Runs the real exporter into a scratch dir and byte-diffs its output.

use std::path::PathBuf;
use std::process::Command;

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..")
}

/// Runs `scripts/export-quota-lua.py --out <tmp>`; fails (never skips) if python is missing.
fn export() -> PathBuf {
    let out = std::env::temp_dir().join(format!("quota-lua-drift-{}", std::process::id()));
    let status = Command::new("python3")
        .arg("-I")
        .arg(repo_root().join("scripts/export-quota-lua.py"))
        .arg("--out")
        .arg(&out)
        .status()
        .expect("python3 must be available to run the quota Lua exporter");
    assert!(status.success(), "export-quota-lua.py failed");
    out
}

#[test]
fn embedded_lua_equals_python_constants() {
    let out = export();
    let reserve = std::fs::read(out.join("reserve.lua")).expect("exported reserve.lua");
    let reconcile = std::fs::read(out.join("reconcile.lua")).expect("exported reconcile.lua");
    let _ = std::fs::remove_dir_all(&out);
    assert!(!reserve.is_empty() && !reconcile.is_empty());
    assert_eq!(
        quota::LUA_RESERVE.as_bytes(),
        reserve.as_slice(),
        "reserve drift"
    );
    assert_eq!(
        quota::LUA_RECONCILE.as_bytes(),
        reconcile.as_slice(),
        "reconcile drift"
    );
}

#[test]
fn committed_lua_files_equal_embedded() {
    let dir = repo_root().join("rust/crates/quota/lua");
    assert_eq!(
        std::fs::read(dir.join("reserve.lua")).unwrap(),
        quota::LUA_RESERVE.as_bytes()
    );
    assert_eq!(
        std::fs::read(dir.join("reconcile.lua")).unwrap(),
        quota::LUA_RECONCILE.as_bytes()
    );
}
