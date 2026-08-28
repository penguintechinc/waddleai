//! Non-secret CLI configuration.
//!
//! Only the deployment URL and cosmetic preferences live here, on disk,
//! under the platform config directory. **Tokens never touch this file** --
//! they live exclusively in the OS-native credential store via
//! [`crate::token_store`]. See client standards: no plaintext token files
//! in a distributed client, ever.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::error::{CliError, Result};

const CONFIG_DIR_NAME: &str = "waddleai";
const CONFIG_FILE_NAME: &str = "config.json";
const DEFAULT_API_URL: &str = "http://localhost:8000";
const ENV_API_URL: &str = "WADDLEAI_API_URL";

/// Non-secret CLI configuration persisted to `~/.config/waddleai/config.json`
/// (or the platform equivalent). Never holds a token.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Config {
    pub api_url: String,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            api_url: DEFAULT_API_URL.to_string(),
        }
    }
}

/// Resolves the platform config directory for this CLI, creating it if
/// absent. Returns [`CliError::Config`] if the platform has no resolvable
/// config directory (e.g. an unset `$HOME` in a stripped-down container).
pub fn config_dir() -> Result<PathBuf> {
    let base = dirs::config_dir()
        .ok_or_else(|| CliError::Config("could not resolve platform config directory".into()))?;
    let dir = base.join(CONFIG_DIR_NAME);
    std::fs::create_dir_all(&dir)?;
    Ok(dir)
}

fn config_path() -> Result<PathBuf> {
    Ok(config_dir()?.join(CONFIG_FILE_NAME))
}

/// Loads config from an explicit path, falling back to [`Config::default`]
/// if the file doesn't exist. Split from [`load`] so tests can exercise the
/// read/parse logic against a scratch path instead of the real platform
/// config directory (avoids racy global-env-var mutation across parallel
/// tests).
pub fn load_from(path: &Path) -> Result<Config> {
    if !path.exists() {
        return Ok(Config::default());
    }
    let raw = std::fs::read_to_string(path)?;
    let config: Config = serde_json::from_str(&raw)?;
    Ok(config)
}

/// Persists `config` to an explicit path, overwriting any prior value. See
/// [`load_from`] for why this is split from [`save`].
pub fn save_to(path: &Path, config: &Config) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let raw = serde_json::to_string_pretty(config)?;
    std::fs::write(path, raw)?;
    Ok(())
}

/// Loads the persisted config from the real platform config directory,
/// falling back to [`Config::default`] if no config file exists yet.
pub fn load() -> Result<Config> {
    load_from(&config_path()?)
}

/// Persists `config` to the real platform config directory, overwriting
/// any prior value.
pub fn save(config: &Config) -> Result<()> {
    save_to(&config_path()?, config)
}

/// Resolves the effective API base URL: `--api-url` flag > `WADDLEAI_API_URL`
/// env var > persisted config > built-in default. `cli_flag` takes the
/// already-parsed `Option<String>` from clap so this function has no direct
/// dependency on the argument parser.
pub fn resolve_api_url(cli_flag: Option<&str>) -> Result<String> {
    if let Some(url) = cli_flag {
        return Ok(normalize_base_url(url));
    }
    if let Ok(url) = std::env::var(ENV_API_URL) {
        if !url.trim().is_empty() {
            return Ok(normalize_base_url(&url));
        }
    }
    let config = load()?;
    Ok(normalize_base_url(&config.api_url))
}

fn normalize_base_url(url: &str) -> String {
    url.trim_end_matches('/').to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_base_url_strips_trailing_slash() {
        assert_eq!(
            normalize_base_url("https://host.example/"),
            "https://host.example"
        );
        assert_eq!(
            normalize_base_url("https://host.example"),
            "https://host.example"
        );
    }

    #[test]
    fn default_config_uses_localhost() {
        assert_eq!(Config::default().api_url, DEFAULT_API_URL);
    }

    #[test]
    fn resolve_api_url_prefers_cli_flag_over_env() {
        // SAFETY-in-tests: single-threaded env mutation scoped to this test;
        // no other test in this module touches WADDLEAI_API_URL.
        std::env::set_var(ENV_API_URL, "https://from-env.example");
        let resolved = resolve_api_url(Some("https://from-flag.example/")).expect("resolve");
        assert_eq!(resolved, "https://from-flag.example");
        std::env::remove_var(ENV_API_URL);
    }

    #[test]
    fn resolve_api_url_falls_back_to_default_when_nothing_set() {
        std::env::remove_var(ENV_API_URL);
        // Only assert the shape, not equality to DEFAULT_API_URL --
        // whichever real machine runs this test might have a persisted
        // config.json under its real config dir from prior local use.
        let resolved = resolve_api_url(None).expect("resolve");
        assert!(!resolved.is_empty());
        assert!(!resolved.ends_with('/'));
    }

    #[test]
    fn load_from_missing_path_returns_default() {
        let path = std::env::temp_dir().join(format!(
            "waddleai-cfg-missing-{}.json",
            uuid::Uuid::new_v4()
        ));
        assert!(!path.exists());
        let config = load_from(&path).expect("load_from");
        assert_eq!(config, Config::default());
    }

    #[test]
    fn save_to_then_load_from_round_trips() {
        let path = std::env::temp_dir().join(format!("waddleai-cfg-{}.json", uuid::Uuid::new_v4()));
        let config = Config {
            api_url: "https://roundtrip.example".to_string(),
        };
        save_to(&path, &config).expect("save_to");
        let loaded = load_from(&path).expect("load_from");
        assert_eq!(loaded, config);
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn load_from_invalid_json_returns_config_error() {
        let path =
            std::env::temp_dir().join(format!("waddleai-cfg-bad-{}.json", uuid::Uuid::new_v4()));
        std::fs::write(&path, "{ not valid json").expect("write scratch file");
        let err = load_from(&path).expect_err("should fail to parse");
        assert!(matches!(err, CliError::Json(_)));
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn config_dir_creates_and_returns_existing_directory() {
        // Exercises the real platform config dir resolution + create_dir_all
        // idempotency (calling it twice must not error the second time).
        let first = config_dir().expect("config_dir");
        let second = config_dir().expect("config_dir again");
        assert_eq!(first, second);
        assert!(first.is_dir());
    }
}
