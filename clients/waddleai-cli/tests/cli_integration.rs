//! End-to-end tests against the actual compiled `waddleai` binary (plan
//! Task 11, Step 1): the `mcp` stdio subcommand must forward JSON-RPC
//! messages to a mock `/mcp` HTTP endpoint and stream the response back on
//! stdout unchanged, and `--version` must print the build-time-injected
//! version string.

use std::io::Write;
use std::process::{Command, Stdio};

use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn binary_path() -> &'static str {
    env!("CARGO_BIN_EXE_waddleai")
}

/// Spawning + waiting on a child process is a blocking call; running the
/// mock HTTP server on a separate worker thread (multi-thread flavor)
/// keeps it able to answer requests from the child while this test thread
/// blocks on the child's stdout.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn mcp_shim_forwards_initialize_and_list_tools_unchanged() {
    let server = MockServer::start().await;

    Mock::given(method("POST"))
        .and(path("/mcp"))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("content-type", "application/json")
                .insert_header("Mcp-Session-Id", "sess-e2e")
                .set_body_raw(
                    r#"{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18"}}"#,
                    "application/json",
                ),
        )
        .up_to_n_times(1)
        .mount(&server)
        .await;

    Mock::given(method("POST"))
        .and(path("/mcp"))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("content-type", "application/json")
                .set_body_raw(
                    r#"{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"search_code"}]}}"#,
                    "application/json",
                ),
        )
        .mount(&server)
        .await;

    let mut child = Command::new(binary_path())
        .arg("mcp")
        .env("WADDLEAI_API_URL", server.uri())
        .env("WADDLEAI_API_KEY", "wa-test-key")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("failed to spawn waddleai binary");

    {
        let stdin = child.stdin.as_mut().expect("child stdin");
        writeln!(stdin, r#"{{"jsonrpc":"2.0","id":1,"method":"initialize"}}"#)
            .expect("write initialize");
        writeln!(stdin, r#"{{"jsonrpc":"2.0","id":2,"method":"tools/list"}}"#)
            .expect("write tools/list");
    }
    // Dropping the child's stdin handle here closes the pipe -- EOF -- so
    // the shim's read loop exits after processing both lines above.
    drop(child.stdin.take());

    let output = child.wait_with_output().expect("wait for child");
    assert!(
        output.status.success(),
        "waddleai mcp exited non-zero: stderr={}",
        String::from_utf8_lossy(&output.stderr)
    );

    let stdout = String::from_utf8(output.stdout).expect("utf8 stdout");
    let lines: Vec<&str> = stdout.lines().filter(|l| !l.trim().is_empty()).collect();
    assert_eq!(
        lines.len(),
        2,
        "expected exactly 2 JSON-RPC response lines, got: {stdout:?}"
    );
    assert_eq!(
        lines[0],
        r#"{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18"}}"#
    );
    assert_eq!(
        lines[1],
        r#"{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"search_code"}]}}"#
    );
}

#[test]
fn version_flag_prints_injected_build_version() {
    let output = Command::new(binary_path())
        .arg("--version")
        .output()
        .expect("failed to run --version");
    assert!(output.status.success());
    let stdout = String::from_utf8(output.stdout).expect("utf8 stdout");

    let pkg_version = env!("CARGO_PKG_VERSION");
    assert!(
        stdout.contains(pkg_version),
        "--version output {stdout:?} missing package version {pkg_version}"
    );

    // The injected version is "{pkg_version}.{epoch}" -- assert the epoch
    // suffix is present and numeric, not just that the literal package
    // version substring showed up.
    let version_token = stdout
        .split_whitespace()
        .find(|tok| tok.starts_with(pkg_version))
        .unwrap_or_else(|| panic!("no token starting with {pkg_version} in {stdout:?}"));
    let suffix = version_token
        .strip_prefix(pkg_version)
        .and_then(|s| s.strip_prefix('.'))
        .unwrap_or_else(|| panic!("version token {version_token:?} missing build-epoch suffix"));
    assert!(
        !suffix.is_empty() && suffix.chars().all(|c| c.is_ascii_digit()),
        "build-epoch suffix {suffix:?} is not a plain epoch number"
    );
}

#[test]
fn missing_login_produces_clean_error_not_a_panic() {
    // No WADDLEAI_API_KEY, and an isolated HOME/keyring-config-dir so this
    // doesn't accidentally read a real logged-in session from the machine
    // running the test.
    let scratch_home =
        std::env::temp_dir().join(format!("waddleai-cli-test-{}", std::process::id()));
    std::fs::create_dir_all(&scratch_home).expect("create scratch home");

    let output = Command::new(binary_path())
        .arg("keys")
        .env("WADDLEAI_API_URL", "http://127.0.0.1:1")
        .env_remove("WADDLEAI_API_KEY")
        .env("HOME", &scratch_home)
        .env("XDG_CONFIG_HOME", scratch_home.join(".config"))
        .output()
        .expect("failed to run keys");

    let _ = std::fs::remove_dir_all(&scratch_home);

    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("Error:"),
        "expected a clean 'Error: ...' line, got: {stderr:?}"
    );
}
