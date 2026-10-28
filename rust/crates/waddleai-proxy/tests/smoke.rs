//! Smoke test: the proxy binary builds and reports a semver on `--version`.
use std::process::Command;

#[test]
fn builds_and_runs_version() {
    let out = Command::new(env!("CARGO_BIN_EXE_waddleai-proxy"))
        .arg("--version")
        .output()
        .expect("binary runs");
    assert!(out.status.success());
    let text = String::from_utf8(out.stdout).expect("utf8 stdout");
    let ver = text.split_whitespace().last().expect("version token");
    let parts: Vec<&str> = ver.split('.').collect();
    assert_eq!(parts.len(), 3, "not semver: {ver}");
    assert!(
        parts.iter().all(|p| p.parse::<u32>().is_ok()),
        "not semver: {ver}"
    );
}
