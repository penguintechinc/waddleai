//! `waddleai mcp` -- a stdio-to-streamable-HTTP JSON-RPC bridge.
//!
//! This is deliberately a **transport adapter only** (per spec §11.5): it
//! forwards each newline-delimited JSON-RPC message read from stdin to the
//! deployment's `/mcp` streamable-HTTP endpoint, and writes the response
//! back to stdout unchanged -- one JSON-RPC message per line either way.
//! No MCP business logic (tool implementations, resource handling) lives
//! here or anywhere in this crate; that all lives server-side in
//! `shared/mcp/`.
//!
//! The streamable-HTTP transport (MCP spec) can answer either with a
//! single `application/json` body or a `text/event-stream` SSE stream
//! carrying one or more JSON-RPC messages in `data:` lines; both are
//! handled here. The `Mcp-Session-Id` response header (set on the
//! `initialize` response) is captured and echoed on every subsequent
//! request, as the spec requires.

use futures_util::StreamExt;
use tokio::io::{AsyncBufReadExt, AsyncWrite, AsyncWriteExt, BufReader};

use crate::error::{CliError, Result};

const MCP_SESSION_HEADER: &str = "Mcp-Session-Id";

/// Runs the bridge until stdin closes (EOF). `token` is a valid bearer
/// token for the target deployment -- callers resolve it via
/// [`crate::auth::ensure_valid_token`] or `WADDLEAI_API_KEY` before calling
/// this.
pub async fn run(api_url: &str, token: &str) -> Result<()> {
    let http = reqwest::Client::new();
    let mcp_url = format!("{api_url}/mcp");

    let stdin = tokio::io::stdin();
    let mut lines = BufReader::new(stdin).lines();
    let mut stdout = tokio::io::stdout();
    let mut session_id: Option<String> = None;

    while let Some(line) = lines.next_line().await? {
        if line.trim().is_empty() {
            continue;
        }
        session_id =
            forward_one_message(&http, &mcp_url, token, session_id, &line, &mut stdout).await?;
    }
    Ok(())
}

async fn forward_one_message(
    http: &reqwest::Client,
    mcp_url: &str,
    token: &str,
    session_id: Option<String>,
    message: &str,
    stdout: &mut (impl AsyncWrite + Unpin),
) -> Result<Option<String>> {
    let mut builder = http
        .post(mcp_url)
        .bearer_auth(token)
        .header(reqwest::header::CONTENT_TYPE, "application/json")
        .header(
            reqwest::header::ACCEPT,
            "application/json, text/event-stream",
        )
        .body(message.to_string());
    if let Some(sid) = &session_id {
        builder = builder.header(MCP_SESSION_HEADER, sid.clone());
    }

    let response = builder.send().await?;

    let next_session_id = response
        .headers()
        .get(MCP_SESSION_HEADER)
        .and_then(|v| v.to_str().ok())
        .map(str::to_string)
        .or(session_id);

    let status = response.status();
    let content_type = response
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();

    if !status.is_success() {
        let body = response.text().await.unwrap_or_default();
        return Err(CliError::Api {
            status: status.as_u16(),
            message: body,
        });
    }

    if content_type.starts_with("text/event-stream") {
        forward_sse_body(response, stdout).await?;
    } else {
        let body = response.bytes().await?;
        write_line(stdout, &body).await?;
    }

    Ok(next_session_id)
}

/// Streams an SSE response, writing the payload of each `data:` line to
/// stdout as its own line -- i.e. re-emitting each JSON-RPC message the
/// server sent, unchanged, without waiting for the stream to close first.
async fn forward_sse_body(
    response: reqwest::Response,
    stdout: &mut (impl AsyncWrite + Unpin),
) -> Result<()> {
    let mut stream = response.bytes_stream();
    let mut buffer = String::new();

    while let Some(chunk) = stream.next().await {
        let chunk = chunk?;
        buffer.push_str(&String::from_utf8_lossy(&chunk));

        while let Some(newline_pos) = buffer.find('\n') {
            let raw_line: String = buffer.drain(..=newline_pos).collect();
            let line = raw_line.trim_end_matches(['\r', '\n']);
            if let Some(data) = line.strip_prefix("data:") {
                let payload = data.trim_start();
                if !payload.is_empty() {
                    write_line(stdout, payload.as_bytes()).await?;
                }
            }
            // event:, id:, retry:, ':' comments, and blank keep-alive
            // lines are SSE framing, not JSON-RPC payload -- intentionally
            // dropped rather than forwarded.
        }
    }
    Ok(())
}

async fn write_line(stdout: &mut (impl AsyncWrite + Unpin), bytes: &[u8]) -> Result<()> {
    stdout.write_all(bytes).await?;
    stdout.write_all(b"\n").await?;
    stdout.flush().await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn forward_one_message_json_response_round_trips_unchanged() {
        let server = wiremock::MockServer::start().await;
        wiremock::Mock::given(wiremock::matchers::method("POST"))
            .and(wiremock::matchers::path("/mcp"))
            .and(wiremock::matchers::header(
                "authorization",
                "Bearer test-token",
            ))
            .respond_with(
                wiremock::ResponseTemplate::new(200)
                    .insert_header("content-type", "application/json")
                    .insert_header("Mcp-Session-Id", "sess-abc")
                    .set_body_raw(
                        r#"{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}"#,
                        "application/json",
                    ),
            )
            .mount(&server)
            .await;

        let http = reqwest::Client::new();
        let mcp_url = format!("{}/mcp", server.uri());
        let mut out: Vec<u8> = Vec::new();
        let session = forward_one_message(
            &http,
            &mcp_url,
            "test-token",
            None,
            r#"{"jsonrpc":"2.0","id":1,"method":"tools/list"}"#,
            &mut out,
        )
        .await
        .expect("forward");

        assert_eq!(session.as_deref(), Some("sess-abc"));
        let written = String::from_utf8(out).expect("utf8");
        assert_eq!(
            written.trim(),
            r#"{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}"#
        );
    }

    #[tokio::test]
    async fn forward_one_message_reuses_session_id_on_subsequent_call() {
        let server = wiremock::MockServer::start().await;
        wiremock::Mock::given(wiremock::matchers::method("POST"))
            .and(wiremock::matchers::path("/mcp"))
            .and(wiremock::matchers::header("Mcp-Session-Id", "sess-abc"))
            .respond_with(
                wiremock::ResponseTemplate::new(200)
                    .insert_header("content-type", "application/json")
                    .set_body_raw(
                        r#"{"jsonrpc":"2.0","id":2,"result":{}}"#,
                        "application/json",
                    ),
            )
            .mount(&server)
            .await;

        let http = reqwest::Client::new();
        let mcp_url = format!("{}/mcp", server.uri());
        let mut out: Vec<u8> = Vec::new();
        forward_one_message(
            &http,
            &mcp_url,
            "test-token",
            Some("sess-abc".to_string()),
            r#"{"jsonrpc":"2.0","id":2,"method":"tools/call"}"#,
            &mut out,
        )
        .await
        .expect("forward");
    }

    #[tokio::test]
    async fn forward_one_message_sse_response_extracts_data_lines() {
        let server = wiremock::MockServer::start().await;
        let sse_body = "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}\n\n";
        wiremock::Mock::given(wiremock::matchers::method("POST"))
            .and(wiremock::matchers::path("/mcp"))
            .respond_with(
                wiremock::ResponseTemplate::new(200)
                    .insert_header("content-type", "text/event-stream")
                    .set_body_raw(sse_body, "text/event-stream"),
            )
            .mount(&server)
            .await;

        let http = reqwest::Client::new();
        let mcp_url = format!("{}/mcp", server.uri());
        let mut out: Vec<u8> = Vec::new();
        forward_one_message(
            &http,
            &mcp_url,
            "test-token",
            None,
            r#"{"jsonrpc":"2.0","id":1,"method":"initialize"}"#,
            &mut out,
        )
        .await
        .expect("forward");

        let written = String::from_utf8(out).expect("utf8");
        assert_eq!(written.trim(), r#"{"jsonrpc":"2.0","id":1,"result":{}}"#);
    }

    #[tokio::test]
    async fn forward_one_message_error_status_surfaces_api_error() {
        let server = wiremock::MockServer::start().await;
        wiremock::Mock::given(wiremock::matchers::method("POST"))
            .and(wiremock::matchers::path("/mcp"))
            .respond_with(wiremock::ResponseTemplate::new(401).set_body_string("unauthorized"))
            .mount(&server)
            .await;

        let http = reqwest::Client::new();
        let mcp_url = format!("{}/mcp", server.uri());
        let mut out: Vec<u8> = Vec::new();
        let err = forward_one_message(&http, &mcp_url, "bad-token", None, "{}", &mut out)
            .await
            .expect_err("should fail");
        assert!(matches!(err, CliError::Api { status: 401, .. }));
    }
}
