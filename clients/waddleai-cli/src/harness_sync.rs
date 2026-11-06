//! Safe MCP configuration rendering for developer harnesses.
//!
//! This module deliberately renders a standalone JSON fragment.  Callers
//! preview it first and must explicitly choose where to merge/write it; the
//! CLI never guesses a client config path or overwrites user configuration.

use std::fs::OpenOptions;
use std::io::Write;
use std::path::Path;

use serde_json::{json, Value};

use crate::error::{CliError, Result};

pub fn render_mcp_config(api_url: &str) -> Result<Value> {
    let base = api_url.trim_end_matches('/');
    if !base.starts_with("https://") && !base.starts_with("http://localhost") {
        return Err(CliError::InvalidInput(
            "harness MCP URL must be HTTPS (localhost is allowed for development)".into(),
        ));
    }
    Ok(json!({
        "mcpServers": {
            "waddleai": {
                "type": "http",
                "url": format!("{base}/mcp"),
                "headers": {"Authorization": "Bearer ${WADDLEAI_API_KEY}"},
                "enabled": false
            }
        }
    }))
}

/// Write a rendered fragment only to an explicit, new destination.
///
/// The CLI deliberately does not infer a harness-specific config path or
/// merge into existing configuration. `create_new` prevents an accidental
/// overwrite if a file appears after a preview.
pub fn write_mcp_config(api_url: &str, output: &Path) -> Result<()> {
    let rendered = serde_json::to_vec_pretty(&render_mcp_config(api_url)?)?;
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(output)?;
    file.write_all(&rendered)?;
    file.write_all(b"\n")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn renders_disabled_environment_backed_mcp_connection() {
        let config = render_mcp_config("https://ai.example/").unwrap();
        let server = &config["mcpServers"]["waddleai"];
        assert_eq!(server["url"], "https://ai.example/mcp");
        assert_eq!(server["enabled"], false);
        assert_eq!(
            server["headers"]["Authorization"],
            "Bearer ${WADDLEAI_API_KEY}"
        );
    }

    #[test]
    fn rejects_insecure_non_local_urls() {
        assert!(render_mcp_config("http://ai.example").is_err());
    }

    #[test]
    fn writes_only_to_a_new_explicit_file() {
        let dir = std::env::temp_dir().join(format!("waddleai-harness-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let output = dir.join("mcp.json");
        write_mcp_config("https://ai.example", &output).unwrap();
        assert!(std::fs::read_to_string(&output)
            .unwrap()
            .contains("\"enabled\": false"));
        assert!(write_mcp_config("https://ai.example", &output).is_err());
        std::fs::remove_file(&output).unwrap();
        std::fs::remove_dir(&dir).unwrap();
    }
}
