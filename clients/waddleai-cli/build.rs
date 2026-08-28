//! Injects `WADDLEAI_VERSION` = `{CARGO_PKG_VERSION}.{build_epoch}` at
//! compile time, matching the project-wide `vMajor.Minor.Patch.build`
//! version convention (see root `CLAUDE.md`).

fn main() {
    let epoch = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let pkg_version = std::env::var("CARGO_PKG_VERSION").unwrap_or_else(|_| "0.0.0".to_string());
    println!("cargo:rustc-env=WADDLEAI_VERSION={pkg_version}.{epoch}");
    println!("cargo:rerun-if-changed=build.rs");
}
